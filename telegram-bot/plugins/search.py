import re
import asyncio
import html
import logging
from time import time

from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait

from config import RESULTS_CHANNEL, SEARCH_REPLY_TTL
from database.db import (
    get_group, add_user, save_dlt_message, search_index, log_search, log_request,
)

logger = logging.getLogger(__name__)

MAX_RESULTS = 20

_EXCLUDED_COMMANDS = [
    "start", "help", "addsource", "connect", "disconnect",
    "connections", "stats", "broadcast", "ping", "verify", "backfill",
    "status", "trending", "requests", "recent",
]

_results_channel_username: str | None = None
_results_channel_resolved: bool = False


async def _get_results_url(bot, message_id: int) -> str:
    global _results_channel_username, _results_channel_resolved
    if not _results_channel_resolved:
        try:
            chat = await bot.get_chat(RESULTS_CHANNEL)
            _results_channel_username = getattr(chat, "username", None)
            _results_channel_resolved = True
        except Exception:
            _results_channel_username = None
    if _results_channel_username:
        return f"https://t.me/{_results_channel_username}/{message_id}"
    numeric_id = str(RESULTS_CHANNEL).replace("-100", "")
    return f"https://t.me/c/{numeric_id}/{message_id}"


async def _send_result(bot, result_chat: int, source_chat: int, message_id: int):
    """Forward one message to the results channel. Returns the forwarded Message or None."""
    try:
        return await bot.forward_messages(
            chat_id=result_chat,
            from_chat_id=source_chat,
            message_ids=message_id,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        # BUG FIX: retry was previously unprotected — a second exception would crash the
        # entire search handler. Now wrapped so we return None gracefully on retry failure.
        try:
            return await bot.forward_messages(
                chat_id=result_chat,
                from_chat_id=source_chat,
                message_ids=message_id,
            )
        except Exception as retry_e:
            logger.warning("Forward retry failed (chat=%d msg=%d): %s",
                           source_chat, message_id, retry_e)
            return None
    except Exception as e:
        logger.warning("Forward failed (chat=%d msg=%d): %s", source_chat, message_id, e)
        return None


async def _auto_delete(no_res_msg, query_msg, delay: int = 60):
    await asyncio.sleep(delay)
    for m in (no_res_msg, query_msg):
        try:
            await m.delete()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

_QUALITY_TAGS = re.compile(
    r'\b('
    r'2160p?|1080p?|720p?|480p?|360p?|'
    r'4k|uhd|hdr10?|hlg|dv|dolby\.?vision|'
    r'hdrip|bluray|blu.?ray|webrip|web.?dl|'
    r'dvdrip|hdtv|bdrip|hd.?cam|'
    r'x264|x265|h\.?264|h\.?265|avc|hevc|'
    r'aac|ac3|dts|mp3|atmos|'
    r'mp4|mkv|avi|mov|wmv|flv|'
    r'english|hindi|tamil|telugu|malayalam|kannada|punjabi|'
    r'dubbed|dual\.?audio|multi|'
    r'sub(?:title(?:d)?)?|'
    r'extended|unrated|remastered|directors?.?cut|theatrical'
    r')\b',
    re.IGNORECASE,
)
_YEAR_RE   = re.compile(r'\b(?:19|20)\d{2}\b')
_NON_ALPHA = re.compile(r'[^a-z0-9\s]')
_SPACES    = re.compile(r'\s+')

_FILE_TYPE_RANK: dict[str, int] = {
    "video":     4,
    "document":  3,
    "animation": 2,
    "photo":     1,
}

MAX_FORWARD = 10


def _extract_title(text: str) -> str:
    """
    Pull the title portion out of a caption for dedup keying.
    Handles "TITLE : Name" structured captions and plain filenames.
    """
    lines = text.splitlines()
    for line in lines[:5]:
        stripped = line.strip()
        if stripped.startswith("title") and ":" in stripped:
            _, _, title_part = stripped.partition(":")
            title = title_part.strip()
            if title:
                return title
    for line in lines:
        stripped = line.strip()
        if stripped:
            return stripped
    return text


def _canonical(text: str) -> str:
    """
    Normalise a title for deduplication grouping.
    Extracts title line first, then strips year/quality/punctuation.
    Keeps season numbers — Season 1 ≠ Season 2.
    """
    t = _extract_title(text.lower().strip())
    t = _YEAR_RE.sub(" ", t)
    t = _QUALITY_TAGS.sub(" ", t)
    t = _NON_ALPHA.sub(" ", t)
    t = _SPACES.sub(" ", t).strip()
    return t


def _deduplicate(hits: list) -> list:
    seen: dict[str, dict] = {}
    order: list[str] = []
    for hit in hits:
        raw  = (hit.get("file_name") or hit.get("text") or "").strip()
        key  = _canonical(raw)
        if not key:
            key = f"__{hit['chat_id']}_{hit['message_id']}"
        rank     = _FILE_TYPE_RANK.get(hit.get("file_type") or "", 0)
        hit_time = hit.get("indexed_at")
        if key not in seen:
            seen[key] = hit
            order.append(key)
        else:
            prev      = seen[key]
            prev_rank = _FILE_TYPE_RANK.get(prev.get("file_type") or "", 0)
            prev_time = prev.get("indexed_at")
            if rank > prev_rank or (
                rank == prev_rank
                and hit_time and prev_time
                and hit_time > prev_time
            ):
                seen[key] = hit
    return [seen[k] for k in order]


# ---------------------------------------------------------------------------
# Request button callback
# ---------------------------------------------------------------------------

@Client.on_callback_query(filters.regex(r"^req_"))
async def request_cb(bot, update):
    """User tapped [📥 Request This] on a no-results message."""
    query = update.data[4:].strip()
    if not query:
        return await update.answer("Something went wrong.", show_alert=True)

    user_id = update.from_user.id if update.from_user else 0
    chat_id = update.message.chat.id if update.message else 0

    is_new = await log_request(query, user_id, chat_id)

    if is_new:
        await update.answer(
            "✅ Requested! The admin has been notified.\n"
            "We'll try to add this content soon.",
            show_alert=True,
        )
    else:
        await update.answer(
            "✅ Already on the request list!\n"
            "You've already requested this title.",
            show_alert=True,
        )


# ---------------------------------------------------------------------------
# Search handler  (bug fix: ~filters.bot prevents bot replies triggering searches)
# ---------------------------------------------------------------------------

@Client.on_message(filters.group & ~filters.bot & ~filters.command(_EXCLUDED_COMMANDS))
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

    user_id = message.from_user.id if message.from_user else 0
    if message.from_user:
        await add_user(message.from_user.id, message.from_user.first_name)

    raw_hits = await search_index(channels, query, limit=MAX_RESULTS)

    if not raw_hits:
        asyncio.create_task(log_search(query, user_id, message.chat.id, found=False))
        cb_query = query[:59]
        no_res = await message.reply(
            f"❌ <b>No results found for:</b> <i>{html.escape(query)}</i>\n\n"
            "Tap below to request this content 👇",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📥 Request This", callback_data=f"req_{cb_query}")
            ]])
        )
        asyncio.create_task(_auto_delete(no_res, message, delay=60))
        return

    asyncio.create_task(log_search(query, user_id, message.chat.id, found=True))

    unique_hits = _deduplicate(raw_hits)
    total_raw   = len(raw_hits)
    total_uniq  = len(unique_hits)
    to_forward  = unique_hits[:MAX_FORWARD]

    sent_msgs = []
    for hit in to_forward:
        fwd = await _send_result(bot, RESULTS_CHANNEL, hit["chat_id"], hit["message_id"])
        if fwd:
            sent_msgs.append(fwd)

    if not sent_msgs:
        return await message.reply(
            "⚠️ Found results but could not forward them.\n"
            "Make sure the bot is admin in the RESULTS_CHANNEL."
        )

    first_url = await _get_results_url(bot, sent_msgs[0].id)

    if total_raw > total_uniq:
        result_line = (
            f"🎬 <b>Found {len(sent_msgs)} unique result(s)</b> for: "
            f"<i>{html.escape(query)}</i>\n"
            f"<i>({total_raw - total_uniq} duplicate(s) removed)</i>"
        )
    else:
        result_line = (
            f"🎬 <b>Found {len(sent_msgs)} result(s)</b> for: "
            f"<i>{html.escape(query)}</i>"
        )

    reply = await message.reply(
        result_line,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Get Results", url=first_url)
        ]])
    )

    expire = time() + SEARCH_REPLY_TTL
    await save_dlt_message(reply, expire)
    await save_dlt_message(message, expire)
    for m in sent_msgs:
        await save_dlt_message(m, expire)
