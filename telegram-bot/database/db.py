import re
import logging
from datetime import datetime
from config import MONGO_URI
from pymongo.errors import DuplicateKeyError
from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

_dbclient = None


def _get_db():
    global _dbclient
    if _dbclient is None:
        if not MONGO_URI:
            raise RuntimeError("MONGO_URI is not set.")
        _dbclient = AsyncIOMotorClient(
            MONGO_URI,
            tlsAllowInvalidCertificates=True,
            tlsAllowInvalidHostnames=True,
            serverSelectionTimeoutMS=15000,
        )
    return _dbclient["CineIndexBot"]


def _cols():
    db = _get_db()
    return db["GROUPS"], db["USERS"], db["INDEX"], db["Auto-Delete"]


# -- Groups -------------------------------------------------------------------

async def add_group(group_id, group_name, user_id, channels=None):
    grp_col, _, _, _ = _cols()
    data = {
        "_id":      group_id,
        "name":     group_name,
        "user_id":  user_id,
        "channels": channels or [],
    }
    try:
        await grp_col.insert_one(data)
    except DuplicateKeyError:
        pass


async def get_group(group_id):
    grp_col, _, _, _ = _cols()
    doc = await grp_col.find_one({"_id": group_id})
    return dict(doc) if doc else None


async def update_group(group_id, new_data):
    grp_col, _, _, _ = _cols()
    await grp_col.update_one({"_id": group_id}, {"$set": new_data})


async def delete_group(group_id):
    grp_col, _, _, _ = _cols()
    await grp_col.delete_one({"_id": group_id})


async def get_groups():
    grp_col, _, _, _ = _cols()
    count = await grp_col.count_documents({})
    lst = await grp_col.find({}).to_list(length=max(int(count), 1))
    return count, lst


# -- Users --------------------------------------------------------------------

async def add_user(user_id, name):
    _, user_col, _, _ = _cols()
    try:
        await user_col.insert_one({"_id": user_id, "name": name})
    except DuplicateKeyError:
        pass


async def get_users():
    _, user_col, _, _ = _cols()
    count = await user_col.count_documents({})
    lst = await user_col.find({}).to_list(length=max(int(count), 1))
    return count, lst


async def delete_user(user_id):
    _, user_col, _, _ = _cols()
    await user_col.delete_one({"_id": user_id})


# -- Message Index ------------------------------------------------------------

async def index_message(chat_id: int, message_id: int, text: str,
                        file_id: str = None, file_type: str = None,
                        file_name: str = ""):
    """Save a channel post to the search index. Upserts by (chat_id, message_id)."""
    _, _, idx_col, _ = _cols()
    doc = {
        "chat_id":    chat_id,
        "message_id": message_id,
        "text":       text.lower(),
        "file_name":  file_name.lower(),
        "file_id":    file_id,
        "file_type":  file_type,
        "indexed_at": datetime.utcnow(),
    }
    await idx_col.update_one(
        {"chat_id": chat_id, "message_id": message_id},
        {"$set": doc},
        upsert=True,
    )


async def delete_index_message(chat_id: int, message_id: int):
    """Remove a message from the index when deleted from the channel."""
    _, _, idx_col, _ = _cols()
    await idx_col.delete_one({"chat_id": chat_id, "message_id": message_id})


async def search_index(channels: list, query: str, limit: int = 50) -> list:
    """Search indexed messages. All words must appear in text or file_name."""
    _, _, idx_col, _ = _cols()
    if not channels:
        return []

    words = query.lower().strip().split()
    if not words:
        return []

    and_conditions = []
    for word in words:
        escaped = re.escape(word)
        and_conditions.append({
            "$or": [
                {"text":      {"$regex": escaped, "$options": "i"}},
                {"file_name": {"$regex": escaped, "$options": "i"}},
            ]
        })

    mongo_filter = {
        "chat_id": {"$in": channels},
        "$and": and_conditions,
    }
    cursor = idx_col.find(mongo_filter).sort("indexed_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_index_count(channels: list = None) -> int:
    """Count indexed messages. Pass channels=None to count all; [] returns 0."""
    _, _, idx_col, _ = _cols()
    if channels is not None and len(channels) == 0:
        return 0
    filt = {"chat_id": {"$in": channels}} if channels is not None else {}
    return await idx_col.count_documents(filt)


async def delete_channel_index(chat_id: int):
    """Wipe all indexed messages for a channel."""
    _, _, idx_col, _ = _cols()
    result = await idx_col.delete_many({"chat_id": chat_id})
    return result.deleted_count


# -- Auto-Delete --------------------------------------------------------------

async def save_dlt_message(message, time):
    _, _, _, dlt_col = _cols()
    await dlt_col.insert_one({
        "chat_id":    message.chat.id,
        "message_id": message.id,
        "time":       time,
    })


async def get_all_dlt_data(time):
    _, _, _, dlt_col = _cols()
    filt = {"time": {"$lte": time}}
    count = await dlt_col.count_documents(filt)
    return await dlt_col.find(filt).to_list(length=max(int(count), 1))


async def delete_all_dlt_data(time):
    _, _, _, dlt_col = _cols()
    await dlt_col.delete_many({"time": {"$lte": time}})


# -- Indexes & setup ----------------------------------------------------------

async def create_indexes():
    try:
        _, _, idx_col, dlt_col = _cols()
        await idx_col.create_index([("chat_id", 1), ("message_id", 1)], unique=True)
        await idx_col.create_index([("chat_id", 1), ("indexed_at", -1)])
        await dlt_col.create_index("time")
        logger.info("Database indexes OK")
    except Exception as e:
        logger.warning("Index warning: %s", e)
