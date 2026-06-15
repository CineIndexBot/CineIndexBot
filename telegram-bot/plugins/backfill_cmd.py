"""
/backfill command — trigger channel backfill from Telegram.

Requires SESSION env var in Railway Variables (optional — only needed for /backfill).
The main bot runs fine without SESSION.

Usage (owner only):
  /backfill                   → backfill all channels connected to this group
  /backfill -100xxxxxxxxxx    → backfill a specific channel
  /backfill all               → backfill every channel across all groups
  /backfill stop              → cancel a running backfill
"""

import asyncio
import logging
import time
from typing import Dict

from pyrogram import Client, filters
from pyrogram.errors import FloodWait, ChatAdminRequired, ChannelPrivate

from config import OWNER_ID, API_ID, API_HASH, SESSION
from database.db import index_message, get_group, get_groups, get_index_count

logger = logging.getLogger(__name__)

_MEDIA_TYPES = ("document", "video", "audio", "animation", "voice", "video_note", "photo")

# Tracks running backfills per chat so /backfill stop works
_running: Dict[int, bool] = {}


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


async def _backfill_one_channel(user, prog_msg, ch_id: int, idx: int,
                                total: int, trigger_chat: int) -> int:
    """
    Index all messages from one channel. Returns count of indexed messages.
    Retries after FloodWait instead of abandoning the channel.
    """
    try:
        chat = await user.get_chat(ch_id)
        name = getattr(chat, "title", str(ch_id))
    except Exception:
        name = str(ch_id)

    already = await get_index_count([ch_id])
    count   = 0
    skipped = 0
    start   = time.time()
    last_edit = start

    await prog_msg.edit(
        f"⏳ <b>[{idx}/{total}] {name}</b>\n"
        f"Already indexed: {already}\n"
        f"Starting…"
    )

    max_retries = 3
    attempt = 0

    while attempt < max_retries:
        attempt += 1
        try:
            async for message in user.get_chat_history(ch_id):
                if not _running.get(trigger_chat):
                    raise asyncio.CancelledError()

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
                    count += 1
                except Exception as e:
                    logger.warning("Index error msg %d: %s", message.id, e)

                now = time.time()
                if now - last_edit >= 8:
                    elapsed = now - start
                    rate = count / elapsed if elapsed > 0 else 0
                    try:
                        await prog_msg.edit(
                            f"⏳ <b>[{idx}/{total}] {name}</b>\n"
                            f"✅ Indexed: <b>{count}</b> | ⏭ Skipped: {skipped}\n"
                            f"⚡ {rate:.1f} msg/s | ⏱ {_fmt(elapsed)}"
                        )
                    except Exception:
                        pass
                    last_edit = now

            # Completed successfully — break retry loop
            break

        except asyncio.CancelledError:
            raise  # Propagate stop signal
        except FloodWait as e:
            wait = e.value + 5
            try:
                await prog_msg.edit(
                    f"⏳ <b>[{idx}/{total}] {name}</b>\n"
                    f"FloodWait {e.value}s — resuming in {wait}s… (attempt {attempt}/{max_retries})"
                )
            except Exception:
                pass
            await asyncio.sleep(wait)
            # Loop continues — retries get_chat_history from beginning
            # Messages already indexed are upserted (no duplicates)
        except (ChatAdminRequired, ChannelPrivate) as e:
            await prog_msg.edit(
                f"❌ <b>[{idx}/{total}] {name}</b>\n"
                f"Cannot access: {e}\n"
                f"Ensure the secondary account is a member."
            )
            await asyncio.sleep(3)
            return 0

    elapsed = time.time() - start
    await prog_msg.edit(
        f"✅ <b>[{idx}/{total}] {name}</b> done!\n"
        f"Indexed: <b>{count}</b> | Skipped: {skipped} | Time: {_fmt(elapsed)}\n"
        f"{'Processing next…' if idx < total else ''}"
    )
    await asyncio.sleep(1)
    return count


async def _do_backfill(bot, prog_msg, channel_ids: list, trigger_chat: int):
    """Run backfill across a list of channels, updating prog_msg with progress."""
    _running[trigger_chat] = True
    grand_total   = 0
    overall_start = time.time()

    try:
        from pyrogram import Client as PyroClient
        async with PyroClient(
            name="backfill_inline",
            session_string=SESSION,
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True,
        ) as user:
            me = await user.get_me()
            await prog_msg.edit(
                f"🔑 Signed in as <b>{me.first_name}</b>\n"
                f"📡 Channels to process: <b>{len(channel_ids)}</b>\n\n"
                f"Starting…"
            )

            for idx, ch_id in enumerate(channel_ids, 1):
                if not _running.get(trigger_chat):
                    await prog_msg.edit("🛑 Backfill cancelled.")
                    return
                try:
                    grand_total += await _backfill_one_channel(
                        user, prog_msg, ch_id, idx, len(channel_ids), trigger_chat
                    )
                except asyncio.CancelledError:
                    await prog_msg.edit("🛑 Backfill cancelled.")
                    return

        total_time = time.time() - overall_start
        await prog_msg.edit(
            f"🎉 <b>Backfill complete!</b>\n\n"
            f"📦 Total indexed: <b>{grand_total}</b> messages\n"
            f"📡 Channels processed: <b>{len(channel_ids)}</b>\n"
            f"⏱ Total time: <b>{_fmt(total_time)}</b>\n\n"
            f"Search is now fully active for all indexed content."
        )

    except Exception as e:
        logger.exception("Backfill task failed: %s", e)
        try:
            await prog_msg.edit(f"❌ Backfill failed: <code>{e}</code>")
        except Exception:
            pass
    finally:
        _running.pop(trigger_chat, None)


@Client.on_message(filters.command("backfill") & filters.user(OWNER_ID))
async def backfill_cmd(bot, message):
    if not SESSION:
        return await message.reply(
            "❌ <b>SESSION not set.</b>\n\n"
            "To use /backfill from Telegram:\n"
            "Railway → Variables → add <code>SESSION</code> = your secondary account's session string.\n\n"
            "<b>Or run the script directly:</b>\n"
            "<code>SESSION='...' python telegram-bot/scripts/backfill.py -100xxx</code>"
        )

    args = message.command[1:]

    # /backfill stop
    if args and args[0].lower() == "stop":
        if message.chat.id in _running:
            _running[message.chat.id] = False
            return await message.reply("🛑 Stop signal sent — backfill will cancel after the current message.")
        return await message.reply("ℹ️ No backfill is currently running in this chat.")

    # Guard: don't start two at once in the same chat
    if _running.get(message.chat.id):
        return await message.reply(
            "⚠️ A backfill is already running.\n"
            "Use <code>/backfill stop</code> to cancel it first."
        )

    # /backfill all
    if args and args[0].lower() == "all":
        _, groups = await get_groups()
        channel_ids = []
        seen: set = set()
        for g in groups:
            for ch_id in g.get("channels", []):
                if ch_id not in seen:
                    channel_ids.append(ch_id)
                    seen.add(ch_id)
        if not channel_ids:
            return await message.reply("📭 No channels connected in any group.")
        prog = await message.reply(
            f"🔄 <b>Global backfill starting…</b>\n"
            f"📡 {len(channel_ids)} unique channel(s) across all groups.\n\n"
            f"Use <code>/backfill stop</code> to cancel."
        )
        asyncio.create_task(_do_backfill(bot, prog, channel_ids, message.chat.id))
        return

    # /backfill -100xxxxxxxxxx [more ids...]
    if args:
        try:
            channel_ids = [int(a) for a in args]
        except ValueError:
            return await message.reply(
                "❌ Invalid channel ID.\n"
                "Usage: <code>/backfill -100xxxxxxxxxx</code>"
            )
        prog = await message.reply(
            f"🔄 <b>Backfill starting…</b>\n"
            f"📡 {len(channel_ids)} channel(s)\n\n"
            f"Use <code>/backfill stop</code> to cancel."
        )
        asyncio.create_task(_do_backfill(bot, prog, channel_ids, message.chat.id))
        return

    # /backfill — use group's connected channels
    group = await get_group(message.chat.id)
    if not group:
        return await message.reply("⚠️ This group is not registered. Use /start first.")

    channel_ids = group.get("channels", [])
    if not channel_ids:
        return await message.reply(
            "📭 No source channels connected to this group.\n"
            "Use: <code>/addsource add -100xxxxxxxxxx</code>"
        )

    prog = await message.reply(
        f"🔄 <b>Backfill starting…</b>\n"
        f"📡 {len(channel_ids)} channel(s) connected to this group.\n\n"
        f"Use <code>/backfill stop</code> to cancel."
    )
    asyncio.create_task(_do_backfill(bot, prog, channel_ids, message.chat.id))
