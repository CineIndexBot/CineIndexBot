import asyncio
import logging
import signal
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


async def _start_bot_with_retry() -> None:
    """Connect the bot, sleeping through any FloodWait on auth."""
    while True:
        try:
            await Bot.start()
            return
        except FloodWait as e:
            wait = e.value + 10
            logger.warning(
                "Telegram FloodWait %ds on login — retrying in %ds...",
                e.value, wait,
            )
            try:
                await Bot.stop()
            except Exception:
                pass
            await asyncio.sleep(wait)
        except Exception:
            raise


async def main():
    t = threading.Thread(target=_start_health_server, daemon=True)
    t.start()

    await create_indexes()
    logger.info("Starting CineIndexBot...")

    await _start_bot_with_retry()

    me = await Bot.get_me()
    logger.info("Bot started: @%s", me.username)

    delete_task    = asyncio.create_task(auto_delete_loop(Bot))
    scheduler_task = asyncio.create_task(scheduled_backfill_loop(Bot))

    stop_event = asyncio.Event()

    def _handle_signal():
        logger.info("Shutdown signal received — stopping gracefully...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass

    logger.info("Bot is running. SIGTERM/Ctrl+C to stop.")
    await stop_event.wait()

    logger.info("Shutting down tasks...")
    for task in (delete_task, scheduler_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await Bot.stop()
    logger.info("Bot stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")
