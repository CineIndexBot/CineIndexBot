import asyncio
import html
import logging
import uuid
from time import time

from pyrogram import Client, filters
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQueryResultArticle, InputTextMessageContent,
)
from pyrogram.errors import FloodWait, MessageNotModified

from config import RESULTS_CHANNEL, SEARCH_REPLY_TTL
from database.db import get_group, add_user, save_dlt_message, search_index

logger = logging.getLogger(__name__)

MAX_RESULTS = 10


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


@Client.on_message(filters.group & ~filters.command([
    "start", "help", "addsource", "connect", "disconnect",
    "connections", "stats", "broadcast", "ping", "verify",
]))
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
            "📭 No source channels connected.\n"
            "Use: <code>/addsource add -100xxxxxxxxxx</code>"
        )

    if message.from_user:
        await add_user(message.from_user.id, message.from_user.first_name)

    # Search the index
    hits = await search_index(channels, query, limit=MAX_RESULTS)

    if not hits:
        no_res = await message.reply(
            f"❌ <b>No results found for:</b> <i>{html.escape(query)}</i>\n\n"
            "Please request the group admin 👇"
        )
        await asyncio.sleep(60)
        try:
            await no_res.delete()
            await message.delete()
        except Exception:
            pass
        return

    # Send each result to RESULTS_CHANNEL, collect forwarded messages
    sent_msgs = []
    for hit in hits:
        fwd = await _send_result(bot, RESULTS_CHANNEL, hit["chat_id"], hit["message_id"])
        if fwd:
            sent_msgs.append(fwd)

    if not sent_msgs:
        return await message.reply("⚠️ Search found results but could not forward them. Check bot permissions.")

    # Build share buttons
    first_id = sent_msgs[0].id
    last_id  = sent_msgs[-1].id
    share_url = (
        f"https://t.me/c/{str(RESULTS_CHANNEL).replace('-100', '')}/{first_id}"
        if first_id == last_id
        else f"https://t.me/c/{str(RESULTS_CHANNEL).replace('-100', '')}/{first_id}?grouped=1"
    )

    reply = await message.reply(
        f"🎬 <b>Found {len(sent_msgs)} result(s) for:</b> <i>{html.escape(query)}</i>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Get Results", url=share_url)
        ]])
    )

    # Schedule auto-delete
    expire = time() + SEARCH_REPLY_TTL
    await save_dlt_message(reply, expire)
    await save_dlt_message(message, expire)
    for m in sent_msgs:
        await save_dlt_message(m, expire)
