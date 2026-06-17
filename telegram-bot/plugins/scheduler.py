"""
Scheduled auto-reindex — runs every 24 hours in the background.

Catches messages the live indexer missed while the bot was down.
Silent (no Telegram progress messages); summary posted to LOG_CHANNEL.
Last-run time persisted in MongoDB so Railway restarts don't reset the clock.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from pyrogram.errors import FloodWait, ChatAdminRequired, ChannelPrivate

from config import LOG_CHANNEL
from database.db import index_message, get_groups, get_config, set_config

logger = logging.getLogger(__name__)

INTERVAL_HOURS = 24
INTERVAL       = INTERVAL_HOURS * 3600
WARMUP_DELAY   = 5 * 60
CONFIG_KEY     = "scheduler_last_run"

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
    Re-index all messages in a channel. Returns (indexed, skipped).

    BUG FIX: counters were not reset before each retry attempt.
    On FloodWait retry the full history is re-iterated from the start,
    so we must reset per-attempt counters and accumulate into totals.
    """
    total_indexed = 0
    total_skipped = 0
    max_retries   = 3

    for attempt in range(1, max_retries + 1):
        attempt_indexed = 0
        attempt_skipped = 0
        try:
            async for message in bot.get_chat_history(ch_id):
                text, file_name, file_id, file_type = _extract(message)
                if not f"{text} {file_name}".strip():
                    attempt_skipped += 1
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
                    attempt_indexed += 1
                except Exception as e:
                    logger.warning("Scheduler index error msg %d: %s", message.id, e)

            return total_indexed + attempt_indexed, total_skipped + attempt_skipped

        except FloodWait as e:
            wait = e.value + 10
            logger.warning(
                "Scheduler FloodWait %ds on channel %d (attempt %d/%d)",
                e.value, ch_id, attempt, max_retries,
            )
            await asyncio.sleep(wait)

        except (ChatAdminRequired, ChannelPrivate) as e:
            logger.warning("Scheduler cannot access channel %d: %s", ch_id, e)
            return total_indexed + attempt_indexed, total_skipped + attempt_skipped

        except asyncio.CancelledError:
            raise

        except Exception as e:
            logger.warning("Scheduler error on channel %d (attempt %d): %s",
                           ch_id, attempt, e)
            return total_indexed + attempt_indexed, total_skipped + attempt_skipped

    return total_indexed, total_skipped


async def _run_full_reindex(bot):
    """Re-index every channel across all groups. Posts summary to LOG_CHANNEL."""
    _, groups = await get_groups()

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
                f"Channels: <b>{len(channel_ids)}</b>",
            )
        except Exception:
            pass

    overall_start   = time.time()
    total_indexed   = 0
    total_skipped   = 0
    failed_channels = []

    for ch_id in channel_ids:
        try:
            ch_idx, ch_skip = await _reindex_channel(bot, ch_id)
            total_indexed += ch_idx
            total_skipped += ch_skip
            logger.info("Scheduler indexed %d from channel %d", ch_idx, ch_id)
        except asyncio.CancelledError:
            logger.info("Scheduled reindex cancelled mid-run.")
            return
        except Exception as e:
            logger.warning("Scheduler channel %d failed: %s", ch_id, e)
            failed_channels.append(ch_id)

    elapsed = time.time() - overall_start
    logger.info("Scheduled reindex complete: %d indexed, %d skipped, %s",
                total_indexed, total_skipped, _fmt(elapsed))

    if LOG_CHANNEL:
        fail_note = (
            f"\n⚠️ {len(failed_channels)} channel(s) failed" if failed_channels else ""
        )
        try:
            await bot.send_message(
                LOG_CHANNEL,
                f"✅ <b>Scheduled reindex complete</b>\n"
                f"Indexed: <b>{total_indexed:,}</b> messages\n"
                f"Channels: <b>{len(channel_ids)}</b>\n"
                f"Time: <b>{_fmt(elapsed)}</b>{fail_note}\n\n"
                f"Next run in <b>{INTERVAL_HOURS}h</b>",
            )
        except Exception:
            pass


async def scheduled_backfill_loop(bot):
    """
    Main scheduler loop. Runs forever until cancelled.
    Reads/writes last-run timestamp from MongoDB so Railway restarts
    don't reset the 24h clock.
    """
    while True:
        try:
            last_run = await get_config(CONFIG_KEY)
            now      = datetime.now(timezone.utc)

            if last_run is None:
                logger.info("Scheduler: first ever run in %ds.", WARMUP_DELAY)
                await asyncio.sleep(WARMUP_DELAY)
            else:
                # Motor returns naive UTC datetimes from MongoDB; normalise before arithmetic
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                elapsed   = (now - last_run).total_seconds()
                remaining = INTERVAL - elapsed
                if remaining > 0:
                    next_dt = now + timedelta(seconds=remaining)
                    logger.info(
                        "Scheduler: last run %s ago. Next at %s UTC.",
                        _fmt(elapsed), next_dt.strftime("%Y-%m-%d %H:%M"),
                    )
                    await asyncio.sleep(remaining)
                else:
                    logger.info("Scheduler: overdue by %s, running now.", _fmt(-remaining))

            await _run_full_reindex(bot)
            await set_config(CONFIG_KEY, datetime.now(timezone.utc))
            await asyncio.sleep(INTERVAL)

        except asyncio.CancelledError:
            logger.info("Scheduled backfill loop cancelled.")
            return
        except Exception as e:
            logger.exception("Scheduler unexpected error: %s — retrying in 1h", e)
            await asyncio.sleep(3600)
