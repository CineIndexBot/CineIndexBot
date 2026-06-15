"""
/backfill command — trigger channel backfill from Telegram.

Requires SESSION env var set in Railway Variables (optional — only needed for this command).
The main bot runs fine without SESSION; it is only used here.

Usage (in Telegram, owner only):
  /backfill                   → backfill all channels connected to this group
  /backfill -100xxxxxxxxxx    → backfill one specific channel
  /backfill all               → backfill every channel across all groups (global)
  /backfill stop              → cancel a running backfill
"""

import asyncio
import logging
import time
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, ChatAdminRequired, ChannelPrivate

from config import OWNER_ID, API_ID, API_HASH, SESSION
from database.db import index_message, get_group, get_groups, get_index_count

logger = logging.getLogger(__name__)

_MEDIA_TYPES = ("document", "video", "audio", "animation", "voice", "video_note", "photo")

# Track running backfill so /backfill stop works
_running: dict[int, bool] = {}   # chat_id → running flag


def _extract(message):
    text = (message.caption or message.text or "").strip()
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
    if s < 60:   return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:   return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


async def _do_backfill(bot, prog_msg, channel_ids: list, trigger_chat: int):
    """Run backfill for a list of channel IDs, editing prog_msg with progress."""
    _running[trigger_chat] = True
    grand_total = 0
    overall_start = time.time()

    try:
        from pyrogram import Client as PyroClient
        async with PyroClient(
            name="backfill_user_inline",
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

                # Resolve channel name
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
                    f"⏳ <b>[{idx}/{len(channel_ids)}] {name}</b>\n"
                    f"Already indexed: {already}\n"
                    f"Indexing…"
                )

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
                                    f"⏳ <b>[{idx}/{len(channel_ids)}] {name}</b>\n"
                                    f"✅ Indexed: <b>{count}</b> | ⏭ Skipped: {skipped}\n"
                                    f"⚡ Speed: {rate:.1f} msg/s | ⏱ {_fmt(elapsed)}"
                                )
                            except Exception:
                                pass
                            last_edit = now

                except asyncio.CancelledError:
                    await prog_msg.edit("🛑 Backfill cancelled.")
                    return
                except FloodWait as e:
                    await prog_msg.edit(f"⏳ FloodWait {e.value}s — waiting…")
                    await asyncio.sleep(e.value + 2)
                except (ChatAdminRequired, ChannelPrivate) as e:
                    await prog_msg.edit(
                        f"❌ Cannot access <code>{ch_id}</code>: {e}\n"
                        f"Make sure the secondary account is a member of this channel."
                    )
                    await asyncio.sleep(3)
                    continue

                elapsed = time.time() - start
                grand_total += count
                await prog_msg.edit(
                    f"✅ <b>[{idx}/{len(channel_ids)}] {name}</b> done!\n"
                    f"Indexed: <b>{count}</b> | Skipped: {skipped} | Time: {_fmt(elapsed)}\n\n"
                    f"{'Processing next channel…' if idx < len(channel_ids) else ''}"
                )
                await asyncio.sleep(1)

        total_time = time.time() - overall_start
        await prog_msg.edit(
            f"🎉 <b>Backfill complete!</b>\n\n"
            f"📦 Total indexed: <b>{grand_total}</b> messages\n"
            f"📡 Channels: <b>{len(channel_ids)}</b>\n"
            f"⏱ Time: <b>{_fmt(total_time)}</b>\n\n"
            f"Search is now fully active for all indexed content."
        )

    except Exception as e:
        logger.exception("Backfill failed: %s", e)
        try:
            await prog_msg.edit(f"❌ Backfill failed: <code>{e}</code>")
        except Exception:
            pass
    finally:
        _running.pop(trigger_chat, None)


@Client.on_message(filters.command("backfill") & filters.user(OWNER_ID))
async def backfill_cmd(bot, message):
    # SESSION check
    if not SESSION:
        return await message.reply(
            "❌ <b>SESSION not set.</b>\n\n"
            "To use /backfill from Telegram, add <code>SESSION</code> to Railway Variables.\n"
            "(Use the same session string from your secondary Telegram account.)\n\n"
            "<b>Or run the script directly on Railway:</b>\n"
            "<code>SESSION='...' python telegram-bot/scripts/backfill.py -100xxx</code>"
        )

    # /backfill stop
    args = message.command[1:]
    if args and args[0].lower() == "stop":
        if message.chat.id in _running:
            _running[message.chat.id] = False
            return await message.reply("🛑 Stop signal sent — backfill will cancel shortly.")
        return await message.reply("ℹ️ No backfill is currently running.")

    # /backfill all  — global across all groups
    if args and args[0].lower() == "all":
        _, groups = await get_groups()
        channel_ids = []
        seen = set()
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

    # /backfill -100xxxxxxxxxx  — specific channel
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

    # /backfill  — use channels connected to this group
    group = await get_group(message.chat.id)
    if not group:
        return await message.reply(
            "⚠️ This group is not registered. Use /start first."
        )
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
