"""channel-post service: bot capability resolution + at-most-once send.

The bot is taken from the ``broadcast_service`` singleton (its ``.bot``); nothing
else is imported from that module — the user-DM broadcast pipeline is untouched.
Sends are history-first and at-most-once: exactly one aiogram call per request,
never auto-retried. Timeouts leave the row in ``sending`` (ambiguous outcome).
"""

from __future__ import annotations

from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from app.cabinet.schemas.channel_posts import ChannelPostButton, ChannelPostMedia
from app.database.crud.channel_post import (
    create_channel_post,
    mark_channel_post_failed,
    mark_channel_post_sent,
)
from app.services.broadcast_service import broadcast_service
from app.services.telegram_rich import SendRichMessage


ERROR_BOT_UNAVAILABLE = 'bot_unavailable'
ERROR_NOT_POSTABLE = 'not_postable'
ERROR_FORBIDDEN = 'forbidden'
ERROR_CHAT_NOT_FOUND = 'chat_not_found'
ERROR_INVALID_HTML = 'invalid_html'
ERROR_STALE_FILE_ID = 'stale_file_id'
ERROR_TOPIC_CLOSED = 'topic_closed'
ERROR_THREAD_NOT_FOUND = 'thread_not_found'
ERROR_RICH_NOT_SUPPORTED = 'rich_not_supported'
ERROR_BAD_REQUEST = 'bad_request'
ERROR_RETRY_AFTER = 'retry_after'
ERROR_TIMEOUT = 'timeout'

_STATUS_CREATOR = 'creator'
_STATUS_ADMIN = 'administrator'
_STATUS_MEMBER = 'member'
_STATUS_RESTRICTED = 'restricted'

_CHAT_CHANNEL = 'channel'
_CHAT_GROUP = 'group'
_CHAT_SUPERGROUP = 'supergroup'


class ChannelPostSendError(Exception):
    """Send/resolve failure carrying a stable code (never raw Telegram text)."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def _require_bot():
    bot = broadcast_service.bot
    if bot is None:
        raise ChannelPostSendError(ERROR_BOT_UNAVAILABLE, 'bot is not initialized')
    return bot


def _status_str(member: Any) -> str:
    return str(getattr(member, 'status', '') or '')


def _compute_can_post(chat: Any, member: Any) -> bool:
    chat_type = str(getattr(chat, 'type', '') or '')
    status = _status_str(member)

    if chat_type == _CHAT_CHANNEL:
        return status == _STATUS_ADMIN and bool(getattr(member, 'can_post_messages', False))

    if chat_type in (_CHAT_GROUP, _CHAT_SUPERGROUP):
        if status in (_STATUS_CREATOR, _STATUS_ADMIN):
            return True
        if status == _STATUS_RESTRICTED:
            return bool(getattr(member, 'can_send_messages', False))
        if status == _STATUS_MEMBER:
            perms = getattr(chat, 'permissions', None)
            return bool(perms is not None and getattr(perms, 'can_send_messages', False))

    return False


async def fetch_chat(input_value: str):
    """Resolve a chat by @username or id (raises TelegramBadRequest if unknown)."""
    bot = _require_bot()
    return await bot.get_chat(input_value)


async def resolve_capability(chat_id_int: int, *, is_allowlisted: bool, chat: Any = None) -> dict:
    """Resolve the bot's effective posting right for a chat (advisory + TOCTOU)."""
    bot = _require_bot()
    member = await bot.get_chat_member(chat_id=chat_id_int, user_id=bot.id)
    if chat is None:
        chat = await bot.get_chat(chat_id_int)
    can_post = _compute_can_post(chat, member)
    return {
        'chat_id': str(chat_id_int),
        'title': getattr(chat, 'title', None),
        'type': str(getattr(chat, 'type', '') or '') or None,
        'username': getattr(chat, 'username', None),
        'is_allowlisted': is_allowlisted,
        'bot_status': _status_str(member) or None,
        'can_post': can_post,
    }


def _classify_bad_request(exc: TelegramBadRequest) -> str:
    text = str(getattr(exc, 'message', '') or '').lower()
    if 'chat not found' in text:
        return ERROR_CHAT_NOT_FOUND
    if 'topic_closed' in text:
        return ERROR_TOPIC_CLOSED
    if 'message thread not found' in text:
        return ERROR_THREAD_NOT_FOUND
    if 'file' in text and ('reference' in text or 'invalid' in text or 'not found' in text):
        return ERROR_STALE_FILE_ID
    if 'parse' in text or 'entities' in text or 'tag' in text or 'html' in text:
        return ERROR_INVALID_HTML
    return ERROR_BAD_REQUEST


_RICH_UNSUPPORTED_MARKERS = ('sendrichmessage', 'rich', 'unsupported', 'not supported', 'method not found')


def _classify_rich_bad_request(exc: TelegramBadRequest) -> str:
    text = str(getattr(exc, 'message', '') or '').lower()
    if any(marker in text for marker in _RICH_UNSUPPORTED_MARKERS):
        return ERROR_RICH_NOT_SUPPORTED
    return _classify_bad_request(exc)


async def _dispatch_send(bot, chat_id_int, message_text, media, keyboard, disable_web_page_preview, message_thread_id):
    if media is None:
        return await bot.send_message(
            chat_id_int,
            message_text,
            parse_mode='HTML',
            reply_markup=keyboard,
            disable_web_page_preview=disable_web_page_preview,
            message_thread_id=message_thread_id,
        )
    send_method = {
        'photo': bot.send_photo,
        'video': bot.send_video,
        'document': bot.send_document,
    }[media.type]
    return await send_method(
        chat_id_int,
        media.file_id,
        caption=message_text,
        parse_mode='HTML',
        reply_markup=keyboard,
        message_thread_id=message_thread_id,
    )


async def send_post(
    db,
    *,
    chat_id_int: int,
    canonical_channel_id: str,
    title: str | None,
    message_text: str | None,
    buttons: list[ChannelPostButton],
    media: ChannelPostMedia | None,
    keyboard,
    disable_web_page_preview: bool,
    idempotency_key: str,
    admin_id: int | None,
    message_thread_id: int | None = None,
    is_rich: bool = False,
):
    """History-first, at-most-once publish. Returns the persisted ChannelPost row.

    Success → ``sent`` + telegram_message_id. Known Telegram errors → ``failed`` +
    stable code. Timeout/ambiguous → leave ``sending`` and raise
    ChannelPostSendError (never auto-retried). ``is_rich`` routes through
    sendRichMessage (no media, no auto-degradation to sendMessage).
    """
    buttons_json = [{'label': b.label, 'url': b.url} for b in buttons] or None
    media_json = {'type': media.type, 'file_id': media.file_id} if media is not None else None

    post = await create_channel_post(
        db,
        channel_id=canonical_channel_id,
        title=title,
        message_text=message_text,
        buttons_json=buttons_json,
        media_json=media_json,
        idempotency_key=idempotency_key,
        admin_id=admin_id,
        message_thread_id=message_thread_id,
        is_rich=is_rich,
    )

    bot = _require_bot()
    try:
        if is_rich:
            sent = await bot(
                SendRichMessage(
                    chat_id=chat_id_int,
                    message_thread_id=message_thread_id,
                    rich_message={'html': message_text, 'skip_entity_detection': True},
                    reply_markup=keyboard,
                )
            )
        else:
            sent = await _dispatch_send(
                bot, chat_id_int, message_text, media, keyboard, disable_web_page_preview, message_thread_id
            )
    except TelegramForbiddenError:
        return await mark_channel_post_failed(db, post, ERROR_FORBIDDEN)
    except TelegramRetryAfter:
        return await mark_channel_post_failed(db, post, ERROR_RETRY_AFTER)
    except TelegramBadRequest as exc:
        code = _classify_rich_bad_request(exc) if is_rich else _classify_bad_request(exc)
        return await mark_channel_post_failed(db, post, code)
    except TimeoutError as exc:
        raise ChannelPostSendError(ERROR_TIMEOUT, 'send timed out; outcome unknown') from exc

    return await mark_channel_post_sent(db, post, sent.message_id)
