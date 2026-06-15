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

    # Health server in background thread (stays up even during FloodWait)
    t = threading.Thread(target=_start_health_server, daemon=True)
    t.start()

    logger.info("Starting CineIndexBot (no SESSION required)...")

    # Retry loop — handles Telegram FloodWait on bot login gracefully
    while True:
        try:
            async with Bot:
                me = await Bot.get_me()
                logger.info("Bot started: @%s", me.username)

                delete_task = asyncio.create_task(auto_delete_loop(Bot))
                try:
                    await asyncio.Event().wait()
                finally:
                    delete_task.cancel()
                    try:
                        await delete_task
                    except asyncio.CancelledError:
                        pass
            break  # Clean exit

        except FloodWait as e:
            wait = e.value + 10
            logger.warning(
                "Telegram FloodWait on login: %ds required. "
                "Sleeping %ds before retry — do NOT restart the service during this wait.",
                e.value, wait,
            )
            await asyncio.sleep(wait)
            logger.info("FloodWait over — retrying login...")

        except Exception as e:
            logger.exception("Bot crashed: %s", e)
            logger.info("Restarting in 30s...")
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
