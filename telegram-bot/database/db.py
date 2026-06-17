import re
import logging
from datetime import datetime, timedelta, timezone
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


def _config_col():
    return _get_db()["CONFIG"]


def _requests_col():
    return _get_db()["REQUESTS"]


# -- Groups -------------------------------------------------------------------

async def add_group(group_id, group_name, user_id, channels=None):
    grp_col, _, _, _ = _cols()
    try:
        await grp_col.insert_one({
            "_id":      group_id,
            "name":     group_name,
            "user_id":  user_id,
            "channels": channels or [],
        })
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
    lst   = await grp_col.find({}).to_list(length=None)
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
    lst   = await user_col.find({}).to_list(length=None)
    return count, lst


async def delete_user(user_id):
    _, user_col, _, _ = _cols()
    await user_col.delete_one({"_id": user_id})


# -- Message Index ------------------------------------------------------------

async def index_message(chat_id: int, message_id: int, text: str,
                        file_id: str = None, file_type: str = None,
                        file_name: str = ""):
    """Upsert a channel post into the search index."""
    _, _, idx_col, _ = _cols()
    await idx_col.update_one(
        {"chat_id": chat_id, "message_id": message_id},
        {"$set": {
            "chat_id":    chat_id,
            "message_id": message_id,
            "text":       text.lower(),
            "file_name":  file_name.lower(),
            "file_id":    file_id,
            "file_type":  file_type,
            "indexed_at": datetime.now(timezone.utc),
        }},
        upsert=True,
    )


async def delete_index_message(chat_id: int, message_id: int):
    _, _, idx_col, _ = _cols()
    await idx_col.delete_one({"chat_id": chat_id, "message_id": message_id})


async def search_index(channels: list, query: str, limit: int = 50) -> list:
    """Full-text search: all query words must appear in text or file_name."""
    _, _, idx_col, _ = _cols()
    if not channels:
        return []
    words = query.lower().strip().split()
    if not words:
        return []
    and_conditions = []
    for word in words:
        escaped = re.escape(word)
        and_conditions.append({"$or": [
            {"text":      {"$regex": escaped, "$options": "i"}},
            {"file_name": {"$regex": escaped, "$options": "i"}},
        ]})
    cursor = idx_col.find(
        {"chat_id": {"$in": channels}, "$and": and_conditions}
    ).sort("indexed_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def get_index_count(channels: list = None) -> int:
    """Count indexed messages. channels=None → all; channels=[] → 0."""
    _, _, idx_col, _ = _cols()
    if channels is not None and len(channels) == 0:
        return 0
    filt = {"chat_id": {"$in": channels}} if channels is not None else {}
    return await idx_col.count_documents(filt)


async def get_last_indexed_time(chat_id: int):
    """Most recent indexed_at for a channel, or None."""
    _, _, idx_col, _ = _cols()
    doc = await idx_col.find_one(
        {"chat_id": chat_id},
        sort=[("indexed_at", -1)],
        projection={"indexed_at": 1},
    )
    return doc["indexed_at"] if doc else None


async def get_recent_messages(channels: list, limit: int = 10) -> list:
    """Return the most recently indexed messages across the given channels."""
    _, _, idx_col, _ = _cols()
    if not channels:
        return []
    cursor = idx_col.find(
        {"chat_id": {"$in": channels}}
    ).sort("indexed_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def delete_channel_index(chat_id: int):
    _, _, idx_col, _ = _cols()
    result = await idx_col.delete_many({"chat_id": chat_id})
    return result.deleted_count


# -- Search Analytics ---------------------------------------------------------

_NORM_RE = re.compile(r'[^a-z0-9\s]')
_WS_RE   = re.compile(r'\s+')


def _normalize_query(q: str) -> str:
    t = q.lower().strip()
    t = _NORM_RE.sub(' ', t)
    t = _WS_RE.sub(' ', t).strip()
    return t


async def log_search(query: str, user_id: int, chat_id: int, found: bool = True):
    col = _search_col()
    try:
        await col.insert_one({
            "query":       query.strip(),
            "query_norm":  _normalize_query(query),
            "user_id":     user_id,
            "chat_id":     chat_id,
            "found":       found,
            "searched_at": datetime.now(timezone.utc),
        })
    except Exception as e:
        logger.warning("log_search failed: %s", e)


async def get_trending(limit: int = 10, days: int = 7) -> list[dict]:
    col   = _search_col()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    pipeline = [
        {"$match": {"searched_at": {"$gte": since}}},
        {"$group": {
            "_id":         "$query_norm",
            "count":       {"$sum": 1},
            "found_sum":   {"$sum": {"$cond": ["$found", 1, 0]}},
            "raw_queries": {"$push": "$query"},
        }},
        {"$sort": {"count": -1}},
        {"$limit": limit},
    ]
    results = await col.aggregate(pipeline).to_list(length=limit)
    out = []
    for r in results:
        raws  = r.get("raw_queries", [])
        label = max(set(raws), key=raws.count) if raws else r["_id"]
        label = label[0].upper() + label[1:] if label else r["_id"]
        count = r["count"]
        out.append({
            "query":     label,
            "count":     count,
            "found_pct": round(r["found_sum"] * 100 / count) if count else 0,
        })
    return out


async def get_search_stats(days: int = 7) -> dict:
    col   = _search_col()
    since = datetime.now(timezone.utc) - timedelta(days=days)
    pipeline = [
        {"$match": {"searched_at": {"$gte": since}}},
        {"$group": {
            "_id":          None,
            "total":        {"$sum": 1},
            "found_total":  {"$sum": {"$cond": ["$found", 1, 0]}},
            "unique_norms": {"$addToSet": "$query_norm"},
        }},
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


# -- Config & Scheduler State -------------------------------------------------

async def get_config(key: str):
    """Retrieve a persisted config value by key. Returns None if not set."""
    col = _config_col()
    doc = await col.find_one({"_id": key})
    return doc["value"] if doc else None


async def set_config(key: str, value):
    """Upsert a config value."""
    col = _config_col()
    await col.update_one(
        {"_id": key},
        {"$set": {"value": value}},
        upsert=True,
    )


async def get_scheduler_status() -> dict:
    """
    Returns scheduler state for /status and /stats display.
    Kept here (not in scheduler.py) to avoid cross-plugin imports in misc.py.
    """
    from plugins.scheduler import CONFIG_KEY, INTERVAL
    last_run = await get_config(CONFIG_KEY)
    if last_run is None:
        return {"last_run": None, "next_run": None}
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)
    next_run = last_run + timedelta(seconds=INTERVAL)
    return {"last_run": last_run, "next_run": next_run}


# -- Content Requests ---------------------------------------------------------

async def log_request(query: str, user_id: int, chat_id: int) -> bool:
    col  = _requests_col()
    norm = _normalize_query(query)
    existing = await col.find_one({"query_norm": norm, "user_id": user_id, "fulfilled": False})
    if existing:
        return False
    await col.insert_one({
        "query":        query.strip(),
        "query_norm":   norm,
        "user_id":      user_id,
        "chat_id":      chat_id,
        "requested_at": datetime.now(timezone.utc),
        "fulfilled":    False,
    })
    return True


async def get_requests(limit: int = 25, fulfilled: bool = False) -> list[dict]:
    """Top content requests grouped by normalised query, sorted by count."""
    col = _requests_col()
    pipeline = [
        {"$match": {"fulfilled": fulfilled}},
        {"$group": {
            "_id":         "$query_norm",
            "count":       {"$sum": 1},
            "raw_queries": {"$push": "$query"},
            "latest":      {"$max": "$requested_at"},
        }},
        {"$sort": {"count": -1, "latest": -1}},
        {"$limit": limit},
    ]
    results = await col.aggregate(pipeline).to_list(length=limit)
    out = []
    for r in results:
        raws  = r.get("raw_queries", [])
        label = max(set(raws), key=raws.count) if raws else r["_id"]
        label = label[0].upper() + label[1:] if label else r["_id"]
        out.append({
            "query":      label,
            "query_norm": r["_id"],
            "count":      r["count"],
            "latest":     r["latest"],
        })
    return out


async def fulfill_request(query_norm: str) -> int:
    """Mark all pending requests for a normalised query as fulfilled."""
    col    = _requests_col()
    result = await col.update_many(
        {"query_norm": query_norm, "fulfilled": False},
        {"$set": {"fulfilled": True, "fulfilled_at": datetime.now(timezone.utc)}},
    )
    return result.modified_count


async def get_request_count(fulfilled: bool = False) -> int:
    """Count unique pending (or fulfilled) titles."""
    col = _requests_col()
    pipeline = [
        {"$match": {"fulfilled": fulfilled}},
        {"$group": {"_id": "$query_norm"}},
        {"$count": "total"},
    ]
    res = await col.aggregate(pipeline).to_list(length=1)
    return res[0]["total"] if res else 0


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
    if count == 0:
        return []
    return await dlt_col.find(filt).to_list(length=None)


async def delete_all_dlt_data(time):
    _, _, _, dlt_col = _cols()
    await dlt_col.delete_many({"time": {"$lte": time}})


# -- Indexes & setup ----------------------------------------------------------

async def create_indexes():
    try:
        _, _, idx_col, dlt_col = _cols()
        srch_col = _search_col()
        req_col  = _requests_col()
        await idx_col.create_index([("chat_id", 1), ("message_id", 1)], unique=True)
        await idx_col.create_index([("chat_id", 1), ("indexed_at", -1)])
        await idx_col.create_index("indexed_at")
        await dlt_col.create_index("time")
        await srch_col.create_index("searched_at")
        await srch_col.create_index("query_norm")
        await req_col.create_index([("query_norm", 1), ("user_id", 1)])
        await req_col.create_index("fulfilled")
        await req_col.create_index("requested_at")
        logger.info("Database indexes OK")
    except Exception as e:
        logger.warning("Index warning: %s", e)
