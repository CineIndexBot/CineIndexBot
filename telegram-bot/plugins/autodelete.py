import asyncio
import logging
from time import time
from pyrogram import Client
from database.db import get_all_dlt_data, delete_all_dlt_data

logger = logging.getLogger(__name__)


async def auto_delete_loop(bot: Client):
    """Background task: delete expired messages every 60 seconds."""
    while True:
        try:
            await asyncio.sleep(60)
            now = time()
            expired = await get_all_dlt_data(now)
            if not expired:
                continue
            for item in expired:
                try:
                    await bot.delete_messages(item["chat_id"], item["message_id"])
                except Exception:
                    pass
            await delete_all_dlt_data(now)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("Auto-delete loop error: %s", e)
