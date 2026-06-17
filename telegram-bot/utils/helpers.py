import logging

logger = logging.getLogger(__name__)

_rc_username: str | None = None
_rc_resolved: bool = False


async def get_results_url(bot, message_id: int, results_channel: int) -> str:
    """Resolve a Telegram message URL for the results channel. Cached after first call."""
    global _rc_username, _rc_resolved
    if not _rc_resolved:
        try:
            chat = await bot.get_chat(results_channel)
            _rc_username = getattr(chat, "username", None)
            _rc_resolved = True
        except Exception:
            _rc_username = None
    if _rc_username:
        return f"https://t.me/{_rc_username}/{message_id}"
    numeric_id = str(results_channel).replace("-100", "")
    return f"https://t.me/c/{numeric_id}/{message_id}"
