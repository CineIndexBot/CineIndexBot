"""
Scheduled auto-reindex — runs every 24 hours in the background.

Catches messages the live indexer missed while the bot was down.
Completely silent (no Telegram progress messages); summary posted to LOG_CHANNEL.
Last-run time is persisted in MongoDB so Railway restarts don't reset the clock.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta

from pyrogram.errors import FloodWait, ChatAdminRequired, ChannelPrivate

from config import LOG_CHANNEL
from database.db import index_message, get_groups, get_config, set_config

logger = logging.getLogger(__name__)

INTERVAL_HOURS  = 24
INTERVAL        = INTERVAL_HOURS * 3600
WARMUP_DELAY    = 5 * 60   # wait 5 min after bot starts before first ever run
CONFIG_KEY      = "scheduler_last_run"

_MEDIA_TYPES = ("document", "video", "audio", "animation", "voice", "video_note", "photo")


def _extract(message):
    text      = (message.caption or message.text or "").strip()
    file_name = ""
    file_id   = None
    file_type = None
    for attr in _MEDIA_TYPES:
        media = getattr(message, attr, None)
        if media:
            file_type = attr
            file_id   = getattr(media, "file_id", None)
            file_name = getattr(media, "file_name", "") or ""
            break
    return text, file_name, file_id, file_type


def _fmt(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


async def _reindex_channel(bot, ch_id: int) -> tuple[int, int]:
    """
    Re-index all messages in a channel. Returns (indexed_count, skipped_count).
    Uses upsert so already-indexed messages are updated, not duplicated.
    Handles FloodWait with up to 3 retries.
    """
    indexed = 0
    skipped = 0
    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            async for message in bot.get_chat_history(ch_id):
                text, file_name, file_id, file_type = _extract(message)
                if not f"{text} {file_name}".strip():
                    skipped += 1
                    continue
                try:
                    await index_message(
                        chat_id=ch_id,
                        message_id=message.id,
                        text=text,
                        file_id=file_id,
                        file_type=file_type,
                        file_name=file_name,
                    )
                    indexed += 1
                except Exception as e:
                    logger.warning("Scheduler index error msg %d: %s", message.id, e)
            return indexed, skipped

        except FloodWait as e:
            wait = e.value + 10
            logger.warning("Scheduler FloodWait %ds on channel %d (attempt %d/%d)",
                           e.value, ch_id, attempt, max_retries)
            await asyncio.sleep(wait)
        except (ChatAdminRequired, ChannelPrivate) as e:
            logger.warning("Scheduler cannot access channel %d: %s", ch_id, e)
            return indexed, skipped
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Scheduler error on channel %d: %s", ch_id, e)
            return indexed, skipped

    return indexed, skipped


async def _run_full_reindex(bot):
    """Re-index every channel across all groups. Posts summary to LOG_CHANNEL."""
    _, groups = await get_groups()

    # Collect unique channel IDs across all groups
    seen: set = set()
    channel_ids = []
    for g in groups:
        for ch_id in g.get("channels", []):
            if ch_id not in seen:
                channel_ids.append(ch_id)
                seen.add(ch_id)

    if not channel_ids:
        logger.info("Scheduled reindex: no channels found, skipping.")
        return

    logger.info("Scheduled reindex starting: %d channels", len(channel_ids))

    if LOG_CHANNEL:
        try:
            await bot.send_message(
                LOG_CHANNEL,
                f"🔄 <b>Scheduled reindex started</b>\n"
                f"Channels: <b>{len(channel_ids)}</b>"
            )
        except Exception:
            pass

    overall_start   = time.time()
    total_indexed   = 0
    total_skipped   = 0
    failed_channels = []

    for ch_id in channel_ids:
        try:
            ch_indexed, ch_skipped = await _reindex_channel(bot, ch_id)
            total_indexed += ch_indexed
            total_skipped += ch_skipped
            logger.info("Scheduler indexed %d from channel %d", ch_indexed, ch_id)
        except asyncio.CancelledError:
            logger.info("Scheduled reindex cancelled mid-run.")
            return
        except Exception as e:
            logger.warning("Scheduler channel %d failed: %s", ch_id, e)
            failed_channels.append(ch_id)

    elapsed = time.time() - overall_start

    logger.info(
        "Scheduled reindex complete: %d indexed, %d skipped, %s",
        total_indexed, total_skipped, _fmt(elapsed)
    )

    if LOG_CHANNEL:
        fail_note = (
            f"\n⚠️ {len(failed_channels)} channel(s) failed"
            if failed_channels else ""
        )
        try:
            await bot.send_message(
                LOG_CHANNEL,
                f"✅ <b>Scheduled reindex complete</b>\n"
                f"Indexed: <b>{total_indexed:,}</b> messages\n"
                f"Channels: <b>{len(channel_ids)}</b>\n"
                f"Time: <b>{_fmt(elapsed)}</b>{fail_note}\n\n"
                f"Next run in <b>{INTERVAL_HOURS}h</b>"
            )
        except Exception:
            pass


async def scheduled_backfill_loop(bot):
    """
    Main scheduler loop. Runs forever until cancelled.

    On startup:
      - Reads last_run from MongoDB.
      - If overdue (or never run): waits WARMUP_DELAY then runs immediately.
      - Otherwise: sleeps until the 24h window is up.
    After each run: records timestamp, sleeps 24h.
    """
    while True:
        try:
            last_run = await get_config(CONFIG_KEY)
            now      = datetime.utcnow()

            if last_run is None:
                # First ever run — give the bot a few minutes to fully settle
                logger.info("Scheduler: no previous run found. First run in %ds.", WARMUP_DELAY)
                await asyncio.sleep(WARMUP_DELAY)
            else:
                elapsed = (now - last_run).total_seconds()
                remaining = INTERVAL - elapsed
                if remaining > 0:
                    next_run_dt = now + timedelta(seconds=remaining)
                    logger.info(
                        "Scheduler: last run %s ago. Next run at %s UTC (~%s).",
                        _fmt(elapsed),
                        next_run_dt.strftime("%Y-%m-%d %H:%M"),
                        _fmt(remaining),
                    )
                    await asyncio.sleep(remaining)
                else:
                    logger.info("Scheduler: overdue by %s, running now.", _fmt(-remaining))

            await _run_full_reindex(bot)
            await set_config(CONFIG_KEY, datetime.utcnow())

            # Sleep until next 24h window
            await asyncio.sleep(INTERVAL)

        except asyncio.CancelledError:
            logger.info("Scheduled backfill loop cancelled.")
            return
        except Exception as e:
            logger.exception("Scheduler unexpected error: %s — retrying in 1h", e)
            await asyncio.sleep(3600)


async def get_scheduler_status() -> dict:
    """
    Returns scheduler state for display in /status.
    {"last_run": datetime|None, "next_run": datetime|None}
    """
    last_run = await get_config(CONFIG_KEY)
    if last_run is None:
        return {"last_run": None, "next_run": None}
    next_run = last_run + timedelta(seconds=INTERVAL)
    return {"last_run": last_run, "next_run": next_run}
