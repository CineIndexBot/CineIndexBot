from config import API_ID, API_HASH, BOT_TOKEN
from pyrogram import Client

Bot = Client(
    name="cine-index-bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
    plugins={"root": "plugins"},
)
