import html
import logging
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.db import get_group, update_group, add_group, get_index_count, delete_channel_index

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "📋 <b>/addsource usage:</b>\n\n"
    "<code>/addsource list</code> — show connected channels\n"
    "<code>/addsource add -100xxxxxxxxxx</code> — add a channel\n"
    "<code>/addsource remove -100xxxxxxxxxx</code> — remove a channel\n\n"
    "⚠️ The bot must be an <b>admin</b> in the channel to index it."
)


async def _owner_check(bot, message, group):
    if not group:
        await message.reply(
            "⚠️ This group is not registered. Ask the owner to use /start first."
        )
        return False
    if message.from_user and message.from_user.id != group.get("user_id"):
        await message.reply("🚫 Only the group owner can manage source channels.")
        return False
    return True


async def _get_title(bot, ch_id: int) -> str:
    try:
        chat = await bot.get_chat(ch_id)
        return getattr(chat, "title", str(ch_id))
    except Exception:
        return str(ch_id)


@Client.on_message(filters.group & filters.command("addsource"))
async def addsource_cmd(bot, message):
    group = await get_group(message.chat.id)
    if not await _owner_check(bot, message, group):
        return

    args = message.command[1:]

    # /addsource  or  /addsource list
    if not args or args[0].lower() == "list":
        channels = group.get("channels", [])
        if not channels:
            return await message.reply(
                "📭 No source channels connected yet.\n\n"
                "Use: <code>/addsource add -100xxxxxxxxxx</code>\n"
                "Then add the bot as admin in that channel."
            )
        lines = ["<b>📡 Source channels:</b>\n"]
        buttons = []
        for i, ch_id in enumerate(channels, 1):
            title = await _get_title(bot, ch_id)
            count = await get_index_count([ch_id])
            lines.append(
                f"{i}. <b>{html.escape(title)}</b> (<code>{ch_id}</code>) — {count} indexed"
            )
            buttons.append([
                InlineKeyboardButton(
                    f"❌ Remove {title[:25]}",
                    callback_data=f"rmsrc_{message.chat.id}_{ch_id}"
                )
            ])
        return await message.reply(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    sub = args[0].lower()

    # /addsource add <id>
    if sub == "add":
        if len(args) < 2:
            return await message.reply("Usage: <code>/addsource add -100xxxxxxxxxx</code>")
        try:
            ch_id = int(args[1])
        except ValueError:
            return await message.reply(
                "❌ Invalid channel ID. Must be a number like <code>-1001234567890</code>."
            )

        channels = group.get("channels", [])
        if ch_id in channels:
            return await message.reply("⚠️ That channel is already a source.")

        # Verify bot can see the channel
        try:
            chat = await bot.get_chat(ch_id)
            title = getattr(chat, "title", str(ch_id))
        except Exception:
            return await message.reply(
                "❌ Cannot access that channel.\n"
                "Make sure the bot is added as <b>admin</b> in the channel first."
            )

        channels.append(ch_id)
        await update_group(message.chat.id, {"channels": channels})
        indexed = await get_index_count([ch_id])
        await message.reply(
            f"✅ <b>{html.escape(title)}</b> added as source.\n"
            f"📦 Already indexed: <b>{indexed}</b> messages\n"
            f"📡 Total sources: <b>{len(channels)}</b>\n\n"
            f"New posts in that channel will be indexed automatically."
        )

    # /addsource remove <id>
    elif sub == "remove":
        if len(args) < 2:
            return await message.reply(
                "Usage: <code>/addsource remove -100xxxxxxxxxx</code>\n"
                "Or use <code>/addsource list</code> to remove with buttons."
            )
        try:
            ch_id = int(args[1])
        except ValueError:
            return await message.reply("❌ Invalid channel ID.")

        channels = group.get("channels", [])
        if ch_id not in channels:
            return await message.reply("⚠️ That channel is not in your sources.")

        channels.remove(ch_id)
        await update_group(message.chat.id, {"channels": channels})
        await message.reply(
            f"✅ Removed <code>{ch_id}</code> from sources.\n"
            f"📡 Remaining: <b>{len(channels)}</b>\n\n"
            f"<i>Note: Existing index data for this channel is kept.\n"
            f"Use /addsource wipe {ch_id} to also delete indexed messages.</i>"
        )

    # /addsource wipe <id>  — owner-only nuclear option
    elif sub == "wipe":
        if len(args) < 2:
            return await message.reply("Usage: <code>/addsource wipe -100xxxxxxxxxx</code>")
        try:
            ch_id = int(args[1])
        except ValueError:
            return await message.reply("❌ Invalid channel ID.")

        deleted = await delete_channel_index(ch_id)
        channels = group.get("channels", [])
        if ch_id in channels:
            channels.remove(ch_id)
            await update_group(message.chat.id, {"channels": channels})
        await message.reply(
            f"🗑 Wiped <b>{deleted}</b> indexed messages for <code>{ch_id}</code>.\n"
            f"Channel also removed from sources."
        )

    else:
        await message.reply(HELP_TEXT)


@Client.on_callback_query(filters.regex(r"^rmsrc_"))
async def remove_source_cb(bot, update):
    parts = update.data.split("_")
    if len(parts) < 3:
        return await update.answer("Invalid.", show_alert=True)

    group_id = int(parts[1])
    ch_id    = int(parts[2])

    group = await get_group(group_id)
    if not group:
        return await update.answer("Group not found.", show_alert=True)
    if update.from_user.id != group.get("user_id"):
        return await update.answer("Only the group owner can do this.", show_alert=True)

    channels = group.get("channels", [])
    if ch_id not in channels:
        await update.answer("Already removed.", show_alert=True)
    else:
        channels.remove(ch_id)
        await update_group(group_id, {"channels": channels})
        await update.answer("✅ Removed!")

    if not channels:
        try:
            await update.message.edit(
                "📭 No source channels. Use <code>/addsource add -100xxxxxxxxxx</code>"
            )
        except Exception:
            pass
        return

    lines = ["<b>📡 Source channels:</b>\n"]
    buttons = []
    for i, cid in enumerate(channels, 1):
        title = await _get_title(bot, cid)
        count = await get_index_count([cid])
        lines.append(f"{i}. <b>{html.escape(title)}</b> (<code>{cid}</code>) — {count} indexed")
        buttons.append([InlineKeyboardButton(
            f"❌ Remove {title[:25]}",
            callback_data=f"rmsrc_{group_id}_{cid}"
        )])
    try:
        await update.message.edit(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        pass
