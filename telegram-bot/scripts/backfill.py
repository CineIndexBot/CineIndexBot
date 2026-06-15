"""
Backfill script — indexes existing messages from a channel into MongoDB.

Requires a USER SESSION string because bots cannot read channel history.
This is a ONE-TIME script. After running it, the main bot handles new posts
automatically without any SESSION.

Usage:
    SESSION="your_session_string" python scripts/backfill.py -100xxxxxxxxxx
    SESSION="your_session_string" python scripts/backfill.py -100xxx1 -100xxx2
"""

import asyncio
import sys
import os
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyrogram import Client
from database.db import index_message, create_indexes
from config import API_ID, API_HASH

SESSION = os.environ.get("SESSION") or os.environ.get("SESSION_SECRET", "")

_MEDIA_TYPES = ("document", "video", "audio", "animation", "voice", "photo")


def _extract(message):
    text = (message.caption or message.text or "").strip()
    file_name = ""
    file_id = None
    file_type = None
    for attr in _MEDIA_TYPES:
        media = getattr(message, attr, None)
        if media:
            file_type = attr
            file_id = getattr(media, "file_id", None)
            file_name = getattr(media, "file_name", "") or ""
            break
    return text, file_name, file_id, file_type


async def backfill_channel(user_client: Client, channel_id: int, limit: int = 0):
    logger.info("Backfilling channel %d (limit=%s)...", channel_id, limit or "all")
    count = 0
    async for message in user_client.get_chat_history(channel_id, limit=limit):
        text, file_name, file_id, file_type = _extract(message)
        combined = f"{text} {file_name}".strip()
        if not combined:
            continue
        await index_message(
            chat_id=channel_id,
            message_id=message.id,
            text=text,
            file_id=file_id,
            file_type=file_type,
            file_name=file_name,
        )
        count += 1
        if count % 500 == 0:
            logger.info("  ...indexed %d messages so far", count)
    logger.info("✅ Done: indexed %d messages from channel %d", count, channel_id)
    return count


async def main():
    if not SESSION:
        print("❌ SESSION env var is required for backfill.")
        print("   export SESSION='your_pyrogram_session_string'")
        sys.exit(1)

    channel_ids = []
    for arg in sys.argv[1:]:
        try:
            channel_ids.append(int(arg))
        except ValueError:
            print(f"⚠️  Skipping invalid channel ID: {arg}")

    if not channel_ids:
        print("Usage: python scripts/backfill.py -100xxxxxxxxxx [-100yyyyy ...]")
        sys.exit(1)

    await create_indexes()

    async with Client(
        name="backfill_session",
        session_string=SESSION,
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
    ) as user:
        logger.info("✅ User session active: %s", (await user.get_me()).phone_number)
        total = 0
        for ch_id in channel_ids:
            total += await backfill_channel(user, ch_id)
        logger.info("🎉 Backfill complete. Total indexed: %d", total)


if __name__ == "__main__":
    asyncio.run(main())
