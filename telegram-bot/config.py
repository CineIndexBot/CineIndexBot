import os
import logging

logger = logging.getLogger(__name__)

API_ID    = int(os.environ.get("API_ID", 0))
API_HASH  = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID  = int(os.environ.get("OWNER_ID", 0))
LOG_CHANNEL  = int(os.environ.get("LOG_CHANNEL", 0))
RESULTS_CHANNEL = int(os.environ.get("RESULTS_CHANNEL", 0))

MONGODB_PASSWORD = os.environ.get("MONGODB_PASSWORD", "")
_raw_uri = os.environ.get("MONGO_URI", "")
if MONGODB_PASSWORD:
    MONGO_URI = (
        f"mongodb+srv://abdulazizshaik521:{MONGODB_PASSWORD}"
        f"@azizthekiller.h74ev.mongodb.net/?appName=Azizthekiller"
    )
elif _raw_uri:
    MONGO_URI = _raw_uri
else:
    MONGO_URI = ""

SEARCH_REPLY_TTL = 15 * 60
WELCOME_TTL      = 120
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", os.environ.get("PORT", 5000)))
PORT        = HEALTH_PORT

# SESSION is only needed for /backfill command and the standalone backfill script.
# The main bot runs perfectly without SESSION.
SESSION = os.environ.get("SESSION", "")

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise RuntimeError("API_ID, API_HASH, and BOT_TOKEN are required. No SESSION needed for this bot.")
