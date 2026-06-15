import asyncio
import logging
import sys
import threading

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

from pyrogram.errors import FloodWait
from client import Bot
from database.db import create_indexes
from plugins.autodelete import auto_delete_loop
from plugins.scheduler import scheduled_backfill_loop


def _start_health_server():
    try:
        from health import app
        from config import HEALTH_PORT
        import waitress
        waitress.serve(app, host="0.0.0.0", port=HEALTH_PORT)
    except ImportError:
        from health import app
        from config import HEALTH_PORT
        app.run(host="0.0.0.0", port=HEALTH_PORT, use_reloader=False)


async def main():
    await create_indexes()

    # Health server stays up permanently (even during FloodWait sleep)
    t = threading.Thread(target=_start_health_server, daemon=True)
    t.start()

    logger.info("Starting CineIndexBot...")

    while True:
        try:
            async with Bot:
                me = await Bot.get_me()
                logger.info("Bot started: @%s", me.username)

                delete_task    = asyncio.create_task(auto_delete_loop(Bot))
                scheduler_task = asyncio.create_task(scheduled_backfill_loop(Bot))

                try:
                    await asyncio.Event().wait()
                finally:
                    for task in (delete_task, scheduler_task):
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
            break

        except FloodWait as e:
            wait = e.value + 10
            logger.warning(
                "Telegram FloodWait %ds on login. Sleeping %ds then retrying...",
                e.value, wait,
            )
            try:
                await Bot.stop()
            except Exception:
                pass
            await asyncio.sleep(wait)
            logger.info("FloodWait over — retrying...")

        except Exception as e:
            logger.exception("Bot crashed: %s", e)
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
