import os
import logging

logger = logging.getLogger(__name__)

API_ID    = int(os.environ.get("API_ID", 0))
API_HASH  = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID  = int(os.environ.get("OWNER_ID", 0))
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", 0))

_rc = os.environ.get("RESULTS_CHANNEL", "0")
try:
    RESULTS_CHANNEL = int(_rc)
except ValueError:
    RESULTS_CHANNEL = _rc

MONGO_URI = os.environ.get("MONGO_URI", "")

SEARCH_REPLY_TTL = 15 * 60
WELCOME_TTL      = 120
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", os.environ.get("PORT", 5000)))
PORT        = HEALTH_PORT

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID, API_HASH, and BOT_TOKEN are required.")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI is required — set the full Atlas connection string in Railway Variables.")
