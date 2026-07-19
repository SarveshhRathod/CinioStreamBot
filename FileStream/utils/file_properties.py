from __future__ import annotations
import re
import logging
from datetime import datetime
from pyrogram import Client
from typing import Any, Optional

from pyrogram.enums import ParseMode, ChatType
from pyrogram.types import Message
from pyrogram.file_id import FileId
from pyrogram.errors import PeerIdInvalid, ChannelInvalid, ChannelPrivate
from FileStream.bot import FileStream
from FileStream.utils.database import db
from FileStream.config import Telegram, Server



async def get_file_ids(client: Client | bool, db_id: str, multi_clients, message) -> Optional[FileId]:
    logging.debug("Starting of get_file_ids")
    file_info = await db.get_file(db_id)
    if (not "file_ids" in file_info) or not client:
        logging.debug("Storing file_id of all clients in DB")
        log_msg = await send_file(FileStream, db_id, file_info['file_id'], message)
        await db.update_file_ids(db_id, await update_file_id(log_msg.id, multi_clients))
        logging.debug("Stored file_id of all clients in DB")
        if not client:
            return
        file_info = await db.get_file(db_id)

    file_id_info = file_info.setdefault("file_ids", {})
    if not str(client.id) in file_id_info:
        logging.debug("Storing file_id in DB")
        log_msg = await send_file(FileStream, db_id, file_info['file_id'], message)
        msg = await client.get_messages(Telegram.FLOG_CHANNEL, log_msg.id)
        media = get_media_from_message(msg)
        file_id_info[str(client.id)] = getattr(media, "file_id", "")
        await db.update_file_ids(db_id, file_id_info)
        logging.debug("Stored file_id in DB")

    logging.debug("Middle of get_file_ids")
    file_id = FileId.decode(file_id_info[str(client.id)])
    setattr(file_id, "file_size", file_info['file_size'])
    setattr(file_id, "mime_type", file_info['mime_type'])
    setattr(file_id, "file_name", file_info['file_name'])
    setattr(file_id, "unique_id", file_info['file_unique_id'])
    logging.debug("Ending of get_file_ids")
    return file_id


def get_media_from_message(message: "Message") -> Any:
    media_types = (
        "audio",
        "document",
        "photo",
        "sticker",
        "animation",
        "video",
        "voice",
        "video_note",
    )
    for attr in media_types:
        media = getattr(message, attr, None)
        if media:
            return media


def get_media_file_size(m):
    media = get_media_from_message(m)
    return getattr(media, "file_size", "None")


def clean_file_name(file_name: str) -> str:
    """
    Smartly prettify a raw file name for captions/links:
    collapses the dots/underscores "scene" release names are full of into
    plain spaces, while keeping the real file extension intact untouched.

    e.g. "My.Movie.Name.2024.1080p.WEB-DL.mkv" -> "My Movie Name 2024 1080p WEB-DL.mkv"
    """
    if not file_name:
        return file_name

    name, dot, ext = file_name.rpartition(".")
    if not dot or not (1 <= len(ext) <= 5) or not ext.isalnum():
        # No real extension present (e.g. no dot, or a trailing-dot artifact)
        name, ext = file_name, ""

    cleaned = re.sub(r"[._]+", " ", name)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -")
    cleaned = cleaned or name  # never end up with an empty name

    return f"{cleaned}.{ext}" if ext else cleaned


def get_name(media_msg: Message | FileId) -> str:
    if isinstance(media_msg, Message):
        media = get_media_from_message(media_msg)
        file_name = getattr(media, "file_name", "")

    elif isinstance(media_msg, FileId):
        file_name = getattr(media_msg, "file_name", "")

    if not file_name:
        if isinstance(media_msg, Message) and media_msg.media:
            media_type = media_msg.media.value
        elif media_msg.file_type:
            media_type = media_msg.file_type.name.lower()
        else:
            media_type = "file"

        formats = {
            "photo": "jpg", "audio": "mp3", "voice": "ogg",
            "video": "mp4", "animation": "mp4", "video_note": "mp4",
            "sticker": "webp"
        }

        ext = formats.get(media_type)
        ext = "." + ext if ext else ""

        date = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        file_name = f"{media_type}-{date}{ext}"

    return clean_file_name(file_name)


def get_file_info(message):
    media = get_media_from_message(message)
    if message.chat.type == ChatType.PRIVATE:
        user_idx = message.from_user.id
    else:
        user_idx = message.chat.id
    return {
        "user_id": user_idx,
        "file_id": getattr(media, "file_id", ""),
        "file_unique_id": getattr(media, "file_unique_id", ""),
        "file_name": get_name(message),
        "file_size": getattr(media, "file_size", 0),
        "mime_type": getattr(media, "mime_type", "None/unknown")
    }


async def update_file_id(msg_id, multi_clients):
    file_ids = {}
    for client_id, client in multi_clients.items():
        log_msg = await client.get_messages(Telegram.FLOG_CHANNEL, msg_id)
        media = get_media_from_message(log_msg)
        file_ids[str(client.id)] = getattr(media, "file_id", "")

    return file_ids


async def send_file(client: Client, db_id, file_id: str, message):
    file_caption = getattr(message, 'caption', None) or get_name(message)
    try:
        log_msg = await client.send_cached_media(chat_id=Telegram.FLOG_CHANNEL, file_id=file_id,
                                                 caption=f'**{file_caption}**')
    except (PeerIdInvalid, ChannelInvalid, ChannelPrivate, ValueError) as e:
        # Pyrogram can only resolve a chat it has already "seen" once (via an
        # update or get_chat). On a fresh session it hasn't seen FLOG_CHANNEL
        # yet, so the very first send fails - force-cache it once and retry.
        try:
            await client.get_chat(Telegram.FLOG_CHANNEL)
            log_msg = await client.send_cached_media(chat_id=Telegram.FLOG_CHANNEL, file_id=file_id,
                                                     caption=f'**{file_caption}**')
        except Exception:
            logging.error(f"Cannot access FLOG_CHANNEL ({Telegram.FLOG_CHANNEL}): {e}")
            raise RuntimeError(
                f"Cannot access FLOG_CHANNEL ({Telegram.FLOG_CHANNEL}). Make sure this "
                f"bot is an ADMIN of that channel and that FLOG_CHANNEL in your env is the "
                f"correct -100... ID (forward any message from the channel to @userinfobot "
                f"to double-check it)."
            ) from e

    if message.chat.type == ChatType.PRIVATE:
        await log_msg.reply_text(
            text=f"**RᴇQᴜᴇꜱᴛᴇᴅ ʙʏ :** [{message.from_user.first_name}](tg://user?id={message.from_user.id})\n**Uꜱᴇʀ ɪᴅ :** `{message.from_user.id}`\n**Fɪʟᴇ ɪᴅ :** `{db_id}`",
            disable_web_page_preview=True, parse_mode=ParseMode.MARKDOWN, quote=True)
    else:
        await log_msg.reply_text(
            text=f"**RᴇQᴜᴇꜱᴛᴇᴅ ʙʏ :** {message.chat.title} \n**Cʜᴀɴɴᴇʟ ɪᴅ :** `{message.chat.id}`\n**Fɪʟᴇ ɪᴅ :** `{db_id}`",
            disable_web_page_preview=True, parse_mode=ParseMode.MARKDOWN, quote=True)

    return log_msg
    # return await client.send_cached_media(Telegram.BIN_CHANNEL, file_id)

