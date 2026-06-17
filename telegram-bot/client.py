import os
from config import API_ID, API_HASH, BOT_TOKEN
from pyrogram import Client

# If SESSION_STRING is set in Railway Variables, reuse it across restarts
# (same auth key = no zombie sessions = Telegram always routes correctly).
# If not set, fall back to in_memory — bot.py will log the string to copy.
_session_string = os.environ.get("SESSION_STRING", "").strip() or None

Bot = Client(
    name="cine-index-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    session_string=_session_string,
    in_memory=not bool(_session_string),
    plugins={"root": "plugins"},
    sleep_threshold=60,
)
