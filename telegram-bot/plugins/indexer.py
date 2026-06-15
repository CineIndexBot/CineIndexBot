import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from database.db import index_message, delete_index_message

logger = logging.getLogger(__name__)

_MEDIA_TYPES = ("document", "video", "audio", "voice", "video_note", "animation", "photo")


def _extract(message: Message):
    """Pull text, file_name, file_id, file_type from any message."""
    text = (message.caption or message.text or "").strip()
    file_name = ""
    file_id   = None
    file_type = None

    for attr in _MEDIA_TYPES:
        media = getattr(message, attr, None)
        if media:
            file_type = attr
            file_id   = getattr(media, "file_id", None)
            file_name = getattr(media, "file_name", "") or ""
            break

    return text, file_name, file_id, file_type


@Client.on_message(filters.channel & ~filters.service)
async def index_channel_post(bot, message: Message):
    """Index every new post in channels where the bot is admin."""
    text, file_name, file_id, file_type = _extract(message)

    if not f"{text} {file_name}".strip():
        return  # Nothing searchable

    try:
        await index_message(
            chat_id=message.chat.id,
            message_id=message.id,
            text=text,
            file_id=file_id,
            file_type=file_type,
            file_name=file_name,
        )
        logger.debug("Indexed msg %d from chat %d", message.id, message.chat.id)
    except Exception as e:
        logger.warning("Failed to index message %d: %s", message.id, e)


@Client.on_edited_message(filters.channel & ~filters.service)
async def reindex_edited(bot, message: Message):
    """Re-index a channel post when its caption/text is edited."""
    await index_channel_post(bot, message)


@Client.on_deleted_messages()
async def remove_from_index(bot, messages):
    """
    Remove deleted channel posts from the index.
    Note: Telegram does not include chat info in deletion updates sent to bots,
    so chat_id is unavailable here. We do a best-effort delete by message_id only
    when chat_id is available (e.g. group deletions), and skip silently otherwise.
    """
    for msg in messages:
        chat_id = getattr(msg.chat, "id", None) if getattr(msg, "chat", None) else None
        if not chat_id:
            continue  # Can't delete without chat_id — skip
        try:
            await delete_index_message(chat_id=chat_id, message_id=msg.id)
            logger.debug("Removed from index: chat=%d msg=%d", chat_id, msg.id)
        except Exception as e:
            logger.debug("Delete-index skip (chat=%s msg=%d): %s", chat_id, msg.id, e)
