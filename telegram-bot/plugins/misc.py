import html
import logging
from datetime import datetime, timezone
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import OWNER_ID, LOG_CHANNEL
from database.db import (
    add_group, get_group, add_user, get_groups, get_users,
    get_index_count, get_last_indexed_time,
    get_trending, get_search_stats, get_scheduler_status,
    get_requests, fulfill_request, get_request_count,
)

logger = logging.getLogger(__name__)

START_TEXT = """👋 <b>Welcome to CineIndexBot!</b>

🔍 <b>How to search:</b>
Just send any movie or series name in the group.

📡 <b>Setup (group owner):</b>
1. Add the bot as <b>admin</b> in your content channel
2. Use <code>/addsource add -100xxxxxxxxxx</code> in the group
3. Search away — no SESSION needed!

📋 <b>Commands:</b>
/addsource list — show connected channels
/addsource add ‹id› — add a source channel
/addsource remove ‹id› — remove a channel
/backfill — index old messages from connected channels
/backfill -100xxx — index a specific channel
/backfill all — index all channels across all groups
/backfill stop — cancel a running backfill
/status — index status + scheduled reindex info
/trending — top 10 searches this week
/requests — pending content requests (owner only)
/stats — bot statistics (owner only)
/ping — check if bot is alive
"""


def _time_ago(dt: datetime) -> str:
    if not dt:
        return "never"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = int((now - dt).total_seconds())
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def _time_until(dt: datetime) -> str:
    if not dt:
        return "unknown"
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    diff = int((dt - now).total_seconds())
    if diff <= 0:
        return "now"
    if diff < 3600:
        return f"in {diff // 60}m"
    if diff < 86400:
        return f"in {diff // 3600}h {(diff % 3600) // 60}m"
    return f"in {diff // 86400}d {(diff % 86400) // 3600}h"


@Client.on_message(filters.command("start"))
async def start(bot, message):
    user = message.from_user
    if user:
        await add_user(user.id, user.first_name)

    if message.chat.type.name in ("GROUP", "SUPERGROUP"):
        group = await get_group(message.chat.id)
        if not group:
            await add_group(
                group_id=message.chat.id,
                group_name=message.chat.title or "",
                user_id=user.id if user else 0,
            )
            if LOG_CHANNEL:
                try:
                    await bot.send_message(
                        LOG_CHANNEL,
                        f"📌 New group registered:\n"
                        f"<b>{message.chat.title}</b> (<code>{message.chat.id}</code>)\n"
                        f"By: {user.mention if user else 'unknown'}",
                    )
                except Exception:
                    pass

    await message.reply(
        START_TEXT,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("ℹ️ Help", callback_data="help_cb")
        ]])
    )


@Client.on_message(filters.command("help"))
async def help_cmd(bot, message):
    await message.reply(START_TEXT)


@Client.on_callback_query(filters.regex("^help_cb$"))
async def help_cb(bot, update):
    await update.answer()
    await update.message.reply(START_TEXT)


@Client.on_message(filters.command("status"))
async def status_cmd(bot, message):
    group = await get_group(message.chat.id)

    if not group:
        return await message.reply(
            "⚠️ This group is not registered yet.\n"
            "Use /start to register it first."
        )

    channels = group.get("channels", [])
    if not channels:
        return await message.reply(
            "📭 No source channels connected.\n"
            "Use: <code>/addsource add -100xxxxxxxxxx</code>"
        )

    lines = ["🤖 <b>Bot:</b> ✅ Connected\n", "<b>📊 Index Status</b>\n"]

    total = 0
    for ch_id in channels:
        try:
            chat = await bot.get_chat(ch_id)
            name = html.escape(getattr(chat, "title", str(ch_id)))
        except Exception:
            name = str(ch_id)

        count    = await get_index_count([ch_id])
        last_dt  = await get_last_indexed_time(ch_id)
        last_str = _time_ago(last_dt)
        total   += count

        lines.append(
            f"📡 <b>{name}</b>\n"
            f"   └ {count:,} indexed | last: {last_str}"
        )

    lines.append(f"\n<b>Total:</b> {total:,} messages indexed")

    if total == 0:
        lines.append("\n💡 Run /backfill to index existing channel history.")

    sched = await get_scheduler_status()
    if sched["next_run"]:
        last_str = _time_ago(sched["last_run"]) if sched["last_run"] else "never"
        next_str = _time_until(sched["next_run"])
        lines.append(f"\n🔄 <b>Auto-reindex:</b> last {last_str} | next {next_str}")
    else:
        lines.append("\n🔄 <b>Auto-reindex:</b> first run in ~5 min")

    # Show pending request count if any
    req_count = await get_request_count()
    if req_count:
        lines.append(f"\n📋 <b>Pending requests:</b> {req_count} title(s) — use /requests")

    await message.reply("\n".join(lines))


@Client.on_message(filters.command("trending"))
async def trending_cmd(bot, message):
    trending = await get_trending(limit=10, days=7)
    stats    = await get_search_stats(days=7)

    if not trending:
        return await message.reply(
            "📭 No search data yet this week.\n"
            "Once users start searching, top titles will appear here."
        )

    medals = ["🥇", "🥈", "🥉"]
    lines  = ["🔥 <b>Top 10 Searches This Week</b>\n"]

    for i, item in enumerate(trending, start=1):
        prefix = medals[i - 1] if i <= 3 else f"{i}."
        label  = html.escape(item["query"])
        count  = item["count"]
        tag    = " <i>(no results)</i>" if item["found_pct"] < 30 else ""
        lines.append(
            f"{prefix} {label} — <b>{count}</b> search{'es' if count != 1 else ''}{tag}"
        )

    total  = stats["total"]
    unique = stats["unique"]
    miss   = total - stats["found_total"]
    lines.append(f"\n📊 {total:,} total searches | {unique:,} unique titles")
    if miss:
        lines.append(
            f"❓ {miss:,} searches returned no results — consider adding those channels"
        )

    await message.reply("\n".join(lines))


@Client.on_message(filters.command("requests") & filters.user(OWNER_ID))
async def requests_cmd(bot, message):
    """
    /requests         — show top pending requests
    /requests done    — show fulfilled requests
    """
    args      = message.command[1:]
    show_done = args and args[0].lower() in ("done", "fulfilled")
    fulfilled = show_done

    items = await get_requests(limit=20, fulfilled=fulfilled)
    total = await get_request_count(fulfilled=fulfilled)

    if not items:
        if fulfilled:
            return await message.reply("📭 No fulfilled requests yet.")
        return await message.reply(
            "📭 No pending content requests yet.\n\n"
            "When users search and get no results, they can tap\n"
            "<b>[📥 Request This]</b> to add a request here."
        )

    heading = "✅ Fulfilled Requests" if fulfilled else "📋 Pending Content Requests"
    lines   = [f"<b>{heading}</b>  ({total} unique title{'s' if total != 1 else ''})\n"]

    buttons = []
    for i, item in enumerate(items, 1):
        label = html.escape(item["query"])
        count = item["count"]
        age   = _time_ago(item["latest"])
        lines.append(
            f"{i}. <b>{label}</b>\n"
            f"   └ {count} request{'s' if count != 1 else ''} | last: {age}"
        )
        if not fulfilled:
            # Truncate query_norm to fit "rfulfill_" (9 chars) + norm within 64-byte limit
            cb_norm = item["query_norm"][:54]
            buttons.append([InlineKeyboardButton(
                f"✅ {item['query'][:30]}",
                callback_data=f"rfulfill_{cb_norm}",
            )])

    if not fulfilled and total > 0:
        lines.append(f"\n<i>Tap ✅ to mark a title as added to your channel.</i>")

    toggle_text = "📋 Show Pending" if fulfilled else "✅ Show Fulfilled"
    toggle_cmd  = "/requests" if fulfilled else "/requests done"
    lines.append(f"\n<i>{toggle_text}: {toggle_cmd}</i>")

    await message.reply(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


@Client.on_callback_query(filters.regex(r"^rfulfill_") & filters.user(OWNER_ID))
async def fulfill_cb(bot, update):
    """Owner tapped ✅ on a request — mark it fulfilled."""
    query_norm = update.data[9:]  # strip "rfulfill_"
    if not query_norm:
        return await update.answer("Invalid.", show_alert=True)

    count = await fulfill_request(query_norm)
    if count:
        await update.answer(
            f"✅ Marked as fulfilled ({count} request{'s' if count != 1 else ''} cleared).",
            show_alert=True,
        )
    else:
        await update.answer("Already fulfilled or not found.", show_alert=True)

    # Refresh the /requests list in the same message
    items = await get_requests(limit=20, fulfilled=False)
    total = await get_request_count(fulfilled=False)

    if not items:
        try:
            await update.message.edit(
                "📋 <b>Pending Content Requests</b>\n\n"
                "✅ All caught up — no pending requests!",
                reply_markup=None,
            )
        except Exception:
            pass
        return

    lines   = [f"<b>📋 Pending Content Requests</b>  ({total} unique title{'s' if total != 1 else ''})\n"]
    buttons = []
    for i, item in enumerate(items, 1):
        label = html.escape(item["query"])
        cnt   = item["count"]
        age   = _time_ago(item["latest"])
        lines.append(
            f"{i}. <b>{label}</b>\n"
            f"   └ {cnt} request{'s' if cnt != 1 else ''} | last: {age}"
        )
        cb_norm = item["query_norm"][:54]
        buttons.append([InlineKeyboardButton(
            f"✅ {item['query'][:30]}",
            callback_data=f"rfulfill_{cb_norm}",
        )])

    lines.append(f"\n<i>Tap ✅ to mark a title as added to your channel.</i>")

    try:
        await update.message.edit(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        pass


@Client.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats(bot, message):
    grp_count, _ = await get_groups()
    usr_count, _ = await get_users()
    idx_count    = await get_index_count()
    week_stats   = await get_search_stats(days=7)
    sched        = await get_scheduler_status()
    req_pending  = await get_request_count(fulfilled=False)
    req_done     = await get_request_count(fulfilled=True)

    sched_line = (
        f"\n🔄 Last auto-reindex: <b>{_time_ago(sched['last_run'])}</b>"
        if sched["last_run"] else "\n🔄 Auto-reindex: <b>not yet run</b>"
    )

    await message.reply(
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 Groups: <b>{grp_count}</b>\n"
        f"👤 Users: <b>{usr_count}</b>\n"
        f"📦 Indexed messages: <b>{idx_count:,}</b>\n"
        f"{sched_line}\n\n"
        f"🔍 Searches this week: <b>{week_stats['total']:,}</b>\n"
        f"🎯 With results: <b>{week_stats['found_total']:,}</b>\n"
        f"❓ No results: <b>{week_stats['total'] - week_stats['found_total']:,}</b>\n\n"
        f"📋 Pending requests: <b>{req_pending}</b> title(s)\n"
        f"✅ Fulfilled requests: <b>{req_done}</b> title(s)"
    )


@Client.on_message(filters.command("ping"))
async def ping(bot, message):
    await message.reply("🏓 Pong!")
