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

    # Health server in background thread
    t = threading.Thread(target=_start_health_server, daemon=True)
    t.start()

    logger.info("Starting CineIndexBot (no SESSION required)...")
    async with Bot:
        me = await Bot.get_me()
        logger.info("✅ Bot started: @%s", me.username)

        # Start auto-delete background task
        delete_task = asyncio.create_task(auto_delete_loop(Bot))
        try:
            await asyncio.Event().wait()
        finally:
            delete_task.cancel()
            try:
                await delete_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
