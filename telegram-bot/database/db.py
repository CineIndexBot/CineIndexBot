import re
import logging
from datetime import datetime, timedelta
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


def _search_col():
    return _get_db()["SEARCHES"]


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


async def get_last_indexed_time(chat_id: int):
    """Return the datetime of the most recently indexed message for a channel, or None."""
    _, _, idx_col, _ = _cols()
    doc = await idx_col.find_one(
        {"chat_id": chat_id},
        sort=[("indexed_at", -1)],
        projection={"indexed_at": 1},
    )
    return doc["indexed_at"] if doc else None


async def delete_channel_index(chat_id: int):
    """Wipe all indexed messages for a channel."""
    _, _, idx_col, _ = _cols()
    result = await idx_col.delete_many({"chat_id": chat_id})
    return result.deleted_count


# -- Search Analytics ---------------------------------------------------------

_NORM_RE = re.compile(r'[^a-z0-9\s]')
_WS_RE   = re.compile(r'\s+')


def _normalize_query(q: str) -> str:
    """Lowercase + strip non-alphanumeric for grouping near-identical queries."""
    t = q.lower().strip()
    t = _NORM_RE.sub(' ', t)
    t = _WS_RE.sub(' ', t).strip()
    return t


async def log_search(query: str, user_id: int, chat_id: int, found: bool = True):
    """
    Record a search event.
    - query_norm: normalized form used for aggregation grouping
    - query: original text stored for display in /trending
    - found: True if results were returned, False if no results
    """
    col = _search_col()
    try:
        await col.insert_one({
            "query":       query.strip(),
            "query_norm":  _normalize_query(query),
            "user_id":     user_id,
            "chat_id":     chat_id,
            "found":       found,
            "searched_at": datetime.utcnow(),
        })
    except Exception as e:
        logger.warning("log_search failed: %s", e)


async def get_trending(limit: int = 10, days: int = 7) -> list[dict]:
    """
    Return top `limit` searches from the last `days` days.
    Each entry: {"query": str, "count": int, "found_pct": int}
    Grouped by query_norm; display label is the most-frequent raw query in that group.
    """
    col = _search_col()
    since = datetime.utcnow() - timedelta(days=days)

    pipeline = [
        {"$match": {"searched_at": {"$gte": since}}},
        {
            "$group": {
                "_id":        "$query_norm",
                "count":      {"$sum": 1},
                "found_sum":  {"$sum": {"$cond": ["$found", 1, 0]}},
                # collect raw queries to pick the most representative display label
                "raw_queries": {"$push": "$query"},
            }
        },
        {"$sort": {"count": -1}},
        {"$limit": limit},
    ]

    results = await col.aggregate(pipeline).to_list(length=limit)

    out = []
    for r in results:
        # Pick the most common raw query in this group as the display label
        raws = r.get("raw_queries", [])
        if raws:
            label = max(set(raws), key=raws.count)
            # Capitalise first letter for cleaner display
            label = label[0].upper() + label[1:] if label else r["_id"]
        else:
            label = r["_id"]

        count     = r["count"]
        found_pct = round(r["found_sum"] * 100 / count) if count else 0
        out.append({"query": label, "count": count, "found_pct": found_pct})

    return out


async def get_search_stats(days: int = 7) -> dict:
    """Return total searches and unique queries in the last `days` days."""
    col = _search_col()
    since = datetime.utcnow() - timedelta(days=days)
    pipeline = [
        {"$match": {"searched_at": {"$gte": since}}},
        {
            "$group": {
                "_id":          None,
                "total":        {"$sum": 1},
                "found_total":  {"$sum": {"$cond": ["$found", 1, 0]}},
                "unique_norms": {"$addToSet": "$query_norm"},
            }
        },
    ]
    res = await col.aggregate(pipeline).to_list(length=1)
    if not res:
        return {"total": 0, "found_total": 0, "unique": 0}
    r = res[0]
    return {
        "total":       r["total"],
        "found_total": r["found_total"],
        "unique":      len(r.get("unique_norms", [])),
    }


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
        srch_col = _search_col()
        await idx_col.create_index([("chat_id", 1), ("message_id", 1)], unique=True)
        await idx_col.create_index([("chat_id", 1), ("indexed_at", -1)])
        await dlt_col.create_index("time")
        await srch_col.create_index("searched_at")
        await srch_col.create_index("query_norm")
        logger.info("Database indexes OK")
    except Exception as e:
        logger.warning("Index warning: %s", e)
