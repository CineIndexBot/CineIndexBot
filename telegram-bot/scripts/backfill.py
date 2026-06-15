"""
Backfill script — indexes existing channel history using the bot itself.

No SESSION or secondary account needed.
The bot must be added as admin to the channel first.

HOW TO RUN on Railway console:
  python telegram-bot/scripts/backfill.py -100xxxxxxxxxx

HOW TO RUN locally:
  export API_ID=...  API_HASH=...  BOT_TOKEN=...  MONGO_URI=...
  python scripts/backfill.py -100xxxxxxxxxx

FLAGS:
  --limit N     Only index the N most recent messages (for testing)
  --dry-run     Count messages without writing to MongoDB
"""

import asyncio
import sys
import os
import time
import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Add parent dir to path so imports work from scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyrogram import Client
from pyrogram.errors import FloodWait, ChatAdminRequired, ChannelPrivate
from database.db import index_message, create_indexes, get_index_count
from config import API_ID, API_HASH, BOT_TOKEN

_MEDIA_TYPES = ("document", "video", "audio", "animation", "voice", "video_note", "photo")


def _extract(message):
    text = (message.caption or message.text or "").strip()
    file_name = ""
    file_id   = None
    file_type = None
    for attr in _MEDIA_TYPES:
        media = getattr(message, attr, None)
        if media:
            file_type = attr
            file_id   = getattr(media, "file_id", None)
            file_name = getattr(media, "file_name", "") or ""
            break
    return text, file_name, file_id, file_type


def _fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


async def backfill_channel(bot, channel_id: int, limit: int = 0, dry_run: bool = False) -> int:
    try:
        chat = await bot.get_chat(channel_id)
        name = getattr(chat, "title", str(channel_id))
    except Exception:
        name = str(channel_id)

    already = await get_index_count([channel_id])
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("Channel : %s (%d)", name, channel_id)
    logger.info("Already : %d messages indexed", already)
    if limit:
        logger.info("Limit   : %d messages", limit)
    if dry_run:
        logger.info("Mode    : DRY RUN (nothing will be saved)")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    count    = 0
    skipped  = 0
    start    = time.time()
    last_log = start

    try:
        async for message in bot.get_chat_history(channel_id, limit=limit or 0):
            text, file_name, file_id, file_type = _extract(message)
            if not f"{text} {file_name}".strip():
                skipped += 1
                continue

            if not dry_run:
                try:
                    await index_message(
                        chat_id=channel_id,
                        message_id=message.id,
                        text=text,
                        file_id=file_id,
                        file_type=file_type,
                        file_name=file_name,
                    )
                except Exception as e:
                    logger.warning("  Save error msg %d: %s", message.id, e)
                    continue

            count += 1
            now = time.time()
            if now - last_log >= 10:
                elapsed = now - start
                rate = count / elapsed if elapsed > 0 else 0
                logger.info(
                    "  Indexed %d | Skipped %d | %.1f msg/s | %s",
                    count, skipped, rate, _fmt_time(elapsed),
                )
                last_log = now

    except FloodWait as e:
        logger.warning("FloodWait %ds — sleeping...", e.value)
        await asyncio.sleep(e.value + 2)
    except ChatAdminRequired:
        logger.error("Not a member/admin of channel %d — make bot an admin first", channel_id)
        return 0
    except ChannelPrivate:
        logger.error("Channel %d is private and bot is not a member", channel_id)
        return 0

    elapsed = time.time() - start
    action = "Counted" if dry_run else "Indexed"
    logger.info("✅ %s %d messages | Skipped %d | Time %s", action, count, skipped, _fmt_time(elapsed))
    return count


async def main(channel_ids: list, limit: int, dry_run: bool):
    if not channel_ids:
        print("\nUsage: python scripts/backfill.py [-h] [--limit N] [--dry-run] channel_id [channel_id ...]")
        print("Example: python scripts/backfill.py -100123456789 -100987654321\n")
        sys.exit(1)

    if not dry_run:
        await create_indexes()

    print(f"\n{'='*50}")
    print(f"  CineIndexBot Backfill Tool")
    print(f"  Channels : {len(channel_ids)}")
    print(f"  Limit    : {limit or 'all messages'}")
    print(f"  Dry run  : {dry_run}")
    print(f"{'='*50}\n")

    async with Client(
        name="backfill_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN,
        in_memory=True,
    ) as bot:
        me = await bot.get_me()
        logger.info("✅ Signed in as bot: @%s", me.username)

        grand_total   = 0
        overall_start = time.time()

        for ch_id in channel_ids:
            grand_total += await backfill_channel(bot, ch_id, limit=limit, dry_run=dry_run)

        elapsed = time.time() - overall_start
        print(f"\n{'='*50}")
        print(f"  {'DRY RUN' if dry_run else 'DONE'}: {grand_total} messages {'counted' if dry_run else 'indexed'}")
        print(f"  Total time: {_fmt_time(elapsed)}")
        print(f"{'='*50}\n")

        if dry_run:
            print("Run without --dry-run to actually index them.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill old channel messages into CineIndexBot's MongoDB index"
    )
    parser.add_argument(
        "channel_ids", nargs="*", type=int,
        help="Channel IDs to backfill (e.g. -100123456789)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Max messages per channel (default: all)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count messages without saving to MongoDB",
    )
    args = parser.parse_args()
    asyncio.run(main(args.channel_ids, args.limit, args.dry_run))
