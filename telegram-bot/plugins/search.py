import re
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

MAX_RESULTS = 20   # fetch more so dedup has enough to work with; we cap forwards at 10

_EXCLUDED_COMMANDS = [
    "start", "help", "addsource", "connect", "disconnect",
    "connections", "stats", "broadcast", "ping", "verify", "backfill",
    "status",
]

# Cache the results channel public username to build correct message links.
# Only cached on success — transient failures retry on next search.
_results_channel_username: str | None = None
_results_channel_resolved: bool = False


async def _get_results_url(bot, message_id: int) -> str:
    global _results_channel_username, _results_channel_resolved
    if not _results_channel_resolved:
        try:
            chat = await bot.get_chat(RESULTS_CHANNEL)
            _results_channel_username = getattr(chat, "username", None)
            _results_channel_resolved = True   # only cache on success
        except Exception:
            _results_channel_username = None
            # leave _results_channel_resolved = False so we retry next time

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


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

_QUALITY_TAGS = re.compile(
    r'\b('
    r'2160p?|1080p?|720p?|480p?|360p?|'
    r'4k|uhd|hdr10?|hlg|'
    r'hdrip|bluray|blu.?ray|webrip|web.?dl|'
    r'dvdrip|hdtv|bdrip|hd.?cam|'
    r'x264|x265|h\.?264|h\.?265|avc|hevc|'
    r'aac|ac3|dts|mp3|'
    r'mp4|mkv|avi|mov|wmv|flv|'
    r'english|hindi|tamil|telugu|dubbed|'
    r'sub(?:title(?:d)?)?|multi|'
    r'extended|unrated|remastered|directors?.?cut|'
    r'(?:season|s)\s*\d+|'
    r'(?:episode|ep?)\s*\d+|'
    r's\d{1,2}e\d{1,2}'
    r')\b',
    re.IGNORECASE,
)
_YEAR_RE   = re.compile(r'\b(?:19|20)\d{2}\b')
_NON_ALPHA = re.compile(r'[^a-z0-9\s]')
_SPACES    = re.compile(r'\s+')

# file_type priority: higher = prefer this hit over others in the same group
_FILE_TYPE_RANK: dict[str, int] = {
    "video":     4,
    "document":  3,
    "animation": 2,
    "photo":     1,
}

MAX_FORWARD = 10   # cap on unique results forwarded to RESULTS_CHANNEL


def _canonical(text: str) -> str:
    """
    Normalise a title string so near-duplicate entries map to the same key.

    Steps:
      1. Lowercase
      2. Strip year (e.g. 2019)
      3. Strip quality/format/language tags (1080p, BluRay, Hindi, mkv …)
      4. Strip non-alphanumeric characters
      5. Collapse whitespace
    """
    t = text.lower()
    t = _YEAR_RE.sub(" ", t)
    t = _QUALITY_TAGS.sub(" ", t)
    t = _NON_ALPHA.sub(" ", t)
    t = _SPACES.sub(" ", t).strip()
    return t


def _deduplicate(hits: list) -> list:
    """
    Group hits by canonical title, keep one best representative per group.

    Best = highest file_type rank first; ties broken by most recent indexed_at.
    Group insertion order follows first occurrence in the ranked hit list,
    so search relevance order is preserved.
    """
    seen: dict[str, dict] = {}
    order: list[str] = []

    for hit in hits:
        raw = (hit.get("file_name") or hit.get("text") or "").strip()
        key = _canonical(raw)
        # If normalisation strips everything (e.g. purely numeric name), use
        # a unique fallback so it is never grouped with another hit.
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
            # Replace if: better file type, or same type but newer post
            if rank > prev_rank or (
                rank == prev_rank
                and hit_time and prev_time
                and hit_time > prev_time
            ):
                seen[key] = hit

    return [seen[k] for k in order]


# ---------------------------------------------------------------------------
# Search handler
# ---------------------------------------------------------------------------

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
            "📭 No source channels connected.\n"
            "Use: <code>/addsource add -100xxxxxxxxxx</code>"
        )

    if message.from_user:
        await add_user(message.from_user.id, message.from_user.first_name)

    # Search the MongoDB index (fetch extra so dedup has enough to work with)
    raw_hits = await search_index(channels, query, limit=MAX_RESULTS)

    if not raw_hits:
        no_res = await message.reply(
            f"❌ <b>No results found for:</b> <i>{html.escape(query)}</i>\n\n"
            "Please request the group admin 👇"
        )
        asyncio.create_task(_auto_delete(no_res, message, delay=60))
        return

    # Deduplicate — one best result per unique title
    unique_hits = _deduplicate(raw_hits)
    total_raw   = len(raw_hits)
    total_uniq  = len(unique_hits)

    # Cap at MAX_FORWARD
    to_forward = unique_hits[:MAX_FORWARD]

    # Forward each unique hit to RESULTS_CHANNEL
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

    # Build reply text
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
