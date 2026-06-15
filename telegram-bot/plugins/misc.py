import logging
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import OWNER_ID, LOG_CHANNEL
from database.db import add_group, get_group, add_user, get_groups, get_users, get_index_count

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
/stats — bot statistics (owner only)
"""


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


@Client.on_message(filters.command("stats") & filters.user(OWNER_ID))
async def stats(bot, message):
    grp_count, _ = await get_groups()
    usr_count, _ = await get_users()
    idx_count    = await get_index_count()
    await message.reply(
        f"📊 <b>Bot Statistics</b>\n\n"
        f"👥 Groups: <b>{grp_count}</b>\n"
        f"👤 Users: <b>{usr_count}</b>\n"
        f"📦 Indexed messages: <b>{idx_count}</b>"
    )


@Client.on_message(filters.command("ping"))
async def ping(bot, message):
    await message.reply("🏓 Pong!")
