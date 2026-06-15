"""
Backfill script — indexes existing messages from channels into MongoDB.

WHY THIS EXISTS:
  The main bot only indexes NEW posts (messages sent after the bot was added
  as admin). This script indexes OLD messages that were posted before setup.

REQUIRES:
  A SESSION string (Pyrogram user account) because Telegram bots cannot read
  channel message history. This is a ONE-TIME script. The main bot never
  needs SESSION after this runs.

HOW TO RUN on Railway:
  1. Go to your Railway service → Settings → "Run Command"
  2. Paste:
       SESSION="your_session_string" python telegram-bot/scripts/backfill.py -100xxxxxxxxxx
  3. Or run multiple channels at once:
       SESSION="..." python telegram-bot/scripts/backfill.py -100xxx1 -100xxx2 -100xxx3

HOW TO RUN locally:
  export SESSION="your_pyrogram_session_string"
  export API_ID=...  API_HASH=...  MONGO_URI=...
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
from config import API_ID, API_HASH

SESSION = os.environ.get("SESSION") or os.environ.get("SESSION_SECRET", "")

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


async def backfill_channel(
    user_client: Client,
    channel_id: int,
    limit: int = 0,
    dry_run: bool = False,
) -> int:
    # Resolve channel name
    try:
        chat = await user_client.get_chat(channel_id)
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
        async for message in user_client.get_chat_history(channel_id, limit=limit or 0):
            text, file_name, file_id, file_type = _extract(message)
            combined = f"{text} {file_name}".strip()

            if not combined:
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
                    logger.warning("  ⚠ Save error msg %d: %s", message.id, e)
                    continue

            count += 1
            now = time.time()
            if now - last_log >= 10:     # progress every 10 seconds
                elapsed = now - start
                rate    = count / elapsed if elapsed > 0 else 0
                logger.info(
                    "  ↳ Indexed %d | Skipped %d | %.1f msg/s | Running %s",
                    count, skipped, rate, _fmt_time(elapsed),
                )
                last_log = now

    except FloodWait as e:
        logger.warning("FloodWait %ds — sleeping...", e.value)
        await asyncio.sleep(e.value + 2)
    except ChatAdminRequired:
        logger.error("❌ Not a member/admin of channel %d — skipping", channel_id)
        return 0
    except ChannelPrivate:
        logger.error("❌ Channel %d is private and account is not a member — skipping", channel_id)
        return 0

    elapsed = time.time() - start
    action  = "Counted" if dry_run else "Indexed"
    logger.info("✅ %s %d messages | Skipped %d | Time %s", action, count, skipped, _fmt_time(elapsed))
    return count


async def main(channel_ids: list, limit: int, dry_run: bool):
    if not SESSION:
        print("\n❌  SESSION env var is required for backfill.")
        print("    The main bot does NOT need SESSION — only this script does.")
        print("\n    Set it like:")
        print('    SESSION="your_pyrogram_session_string" python scripts/backfill.py -100xxx\n')
        sys.exit(1)

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
        name="backfill_user",
        session_string=SESSION,
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
    ) as user:
        me = await user.get_me()
        logger.info("✅ Signed in as: %s (%s)", me.first_name, me.phone_number)

        grand_total = 0
        overall_start = time.time()

        for ch_id in channel_ids:
            grand_total += await backfill_channel(user, ch_id, limit=limit, dry_run=dry_run)

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
