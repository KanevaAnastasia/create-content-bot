"""Publish one approved photo article using the existing owner Telegram session."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import os

from . import article_store as store
from .article_generation import utf16_length


class NotSubmitted(RuntimeError):
    """Failure before the channel send; retry cannot duplicate a post."""


class ReconciliationNeeded(RuntimeError):
    """Existing channel content is ambiguous; sending again is unsafe."""


@asynccontextmanager
async def channel_client(config):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from .syntax_telethon_fallback import _acquire_syntax_session_candidate, _release_syntax_session_candidate

    candidate = await asyncio.to_thread(_acquire_syntax_session_candidate, 2)
    if candidate is None:
        raise ValueError("Telegram сейчас занят генерацией. Попробуйте позже.")
    client = None
    try:
        session = StringSession(candidate.value) if candidate.kind == "string" else candidate.value
        client = TelegramClient(session, int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"])
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("Нужна авторизованная сессия Telegram владельца.")
        me = await client.get_me()
        if config.get("publisher_id") is not None and me.id != config["publisher_id"]:
            raise ValueError("Сессия принадлежит другому аккаунту. Публикация остановлена.")
        channel = await client.get_entity(config["username"])
        if channel.id != config["channel_id"]:
            raise ValueError("Адрес канала изменился. Публикация остановлена.")
        if not (channel.creator or getattr(channel.admin_rights, "post_messages", False)):
            raise ValueError("У аккаунта нет права публикации в канале.")
        if config.get("publisher_id") is None:
            # Bind only the verified creator of this exact channel. This lets
            # setup avoid competing with an occupied generation session.
            if not channel.creator:
                raise ValueError("Для первой публикации нужна сессия владельца канала.")
            with store.transaction() as data:
                if data.get("channel_id") != channel.id or data.get("publisher_id") not in (None, me.id):
                    raise ValueError("Настройки аккаунта изменились. Публикация остановлена.")
                data["publisher_id"] = me.id
        yield client, channel, me
    finally:
        try:
            if client is not None:
                await client.disconnect()
        finally:
            _release_syntax_session_candidate(candidate)


async def find_post(client, channel, row):
    if row.get("channel_message_id"):
        msg = await client.get_messages(channel, ids=row["channel_message_id"])
        if msg and msg.raw_text == row["text"] and msg.photo:
            return msg.id
    started = datetime.fromisoformat(row["publish_started_at"])
    matches = []
    async for msg in client.iter_messages(channel, limit=100):
        if msg.date < started:
            break
        if msg.raw_text == row["text"] and msg.photo:
            matches.append(msg.id)
    if len(matches) > 1:
        raise ReconciliationNeeded("В канале несколько совпадающих постов. Нужна ручная проверка.")
    return matches[0] if matches else None


async def publish(config, row, *, check_only=False):
    submission = {"started": False}
    try:
        async with channel_client(config) as (client, channel, me):
            return await _publish_connected(client, channel, me, row, submission, check_only=check_only)
    except Exception as exc:
        if not submission["started"] and not check_only and not isinstance(exc, ReconciliationNeeded):
            raise NotSubmitted("Пост не отправлен. Можно повторить публикацию.") from exc
        raise


async def _publish_connected(client, channel, me, row, submission, *, check_only):
    existing = await find_post(client, channel, row)
    if existing:
        submission["started"] = True
        return existing
    if check_only:
        return None
    limit = 4096 if me.premium else 1024
    if utf16_length(row["text"]) > limit:
        raise ValueError("Текст не помещается в подпись текущего Telegram-аккаунта. Нужна редактура.")
    from telethon import functions, types
    uploaded = await client.upload_file(row["images"]["horizontal"])
    submission["started"] = True
    result = await client(functions.messages.SendMediaRequest(
        peer=channel, media=types.InputMediaUploadedPhoto(file=uploaded), message=row["text"],
        random_id=row["publish_random_id"],
    ))
    # Persist the provider id before fetching confirmation; a timeout cannot erase it.
    for update in getattr(result, "updates", []):
        message = getattr(update, "message", None)
        if (message is not None and getattr(message.peer_id, "channel_id", None) == channel.id
                and message.message == row["text"] and message.photo):
            store.checkpoint(row["id"], channel_message_id=message.id)
            confirmed = await client.get_messages(channel, ids=message.id)
            if confirmed and confirmed.raw_text == row["text"] and confirmed.photo:
                return confirmed.id
    return await find_post(client, channel, row)
