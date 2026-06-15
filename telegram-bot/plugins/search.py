import asyncio
import html
import logging
from time import time

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

from config import RESULTS_CHANNEL, SEARCH_REPLY_TTL
from database.db import get_group, add_user, save_dlt_message, search_index

logger = logging.getLogger(__name__)

MAX_RESULTS = 10

_EXCLUDED_COMMANDS = [
    "start", "help", "addsource", "connect", "disconnect",
    "connections", "stats", "broadcast", "ping", "verify", "backfill",
]

# Cache the results channel public username to build correct message links
_results_channel_username: str | None = None
_results_channel_resolved: bool = False


async def _get_results_url(bot, message_id: int) -> str:
    global _results_channel_username, _results_channel_resolved
    if not _results_channel_resolved:
        try:
            chat = await bot.get_chat(RESULTS_CHANNEL)
            _results_channel_username = getattr(chat, "username", None)
        except Exception:
            _results_channel_username = None
        _results_channel_resolved = True

    if _results_channel_username:
        return f"https://t.me/{_results_channel_username}/{message_id}"
    # Private channel — use numeric ID format
    numeric_id = str(RESULTS_CHANNEL).replace("-100", "")
    return f"https://t.me/c/{numeric_id}/{message_id}"


async def _send_result(bot, result_chat: int, source_chat: int, message_id: int):
    """Forward a single indexed message to the results channel."""
    try:
        return await bot.forward_messages(
            chat_id=result_chat,
            from_chat_id=source_chat,
            message_ids=message_id,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await bot.forward_messages(
            chat_id=result_chat,
            from_chat_id=source_chat,
            message_ids=message_id,
        )
    except Exception as e:
        logger.warning("Forward failed (chat=%d msg=%d): %s", source_chat, message_id, e)
        return None


async def _auto_delete(no_res_msg, query_msg, delay: int = 60):
    """Delete no-results reply and the original query after delay seconds."""
    await asyncio.sleep(delay)
    for m in (no_res_msg, query_msg):
        try:
            await m.delete()
        except Exception:
            pass


@Client.on_message(filters.group & ~filters.command(_EXCLUDED_COMMANDS))
async def search(bot, message):
    if not message.text and not message.caption:
        return

    query = (message.text or message.caption or "").strip()
    if not query or len(query) < 2:
        return

    group = await get_group(message.chat.id)
    if not group:
        return

    channels = group.get("channels", [])
    if not channels:
        return await message.reply(
            "📭 No source channels connected.
"
            "Use: <code>/addsource add -100xxxxxxxxxx</code>"
        )

    if message.from_user:
        await add_user(message.from_user.id, message.from_user.first_name)

    # Search the MongoDB index
    hits = await search_index(channels, query, limit=MAX_RESULTS)

    if not hits:
        no_res = await message.reply(
            f"❌ <b>No results found for:</b> <i>{html.escape(query)}</i>

"
            "Please request the group admin 👇"
        )
        asyncio.create_task(_auto_delete(no_res, message, delay=60))
        return

    # Forward each hit to RESULTS_CHANNEL
    sent_msgs = []
    for hit in hits:
        fwd = await _send_result(bot, RESULTS_CHANNEL, hit["chat_id"], hit["message_id"])
        if fwd:
            sent_msgs.append(fwd)

    if not sent_msgs:
        return await message.reply(
            "⚠️ Found results but could not forward them.
"
            "Make sure the bot is admin in the RESULTS_CHANNEL."
        )

    first_url = await _get_results_url(bot, sent_msgs[0].id)

    reply = await message.reply(
        f"🎬 <b>Found {len(sent_msgs)} result(s) for:</b> <i>{html.escape(query)}</i>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Get Results", url=first_url)
        ]])
    )

    expire = time() + SEARCH_REPLY_TTL
    await save_dlt_message(reply, expire)
    await save_dlt_message(message, expire)
    for m in sent_msgs:
        await save_dlt_message(m, expire)
