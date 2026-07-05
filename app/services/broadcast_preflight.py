"""Пре-флайт валидация рассылки перед массовым фан-аутом.

Инцидент: реальная рассылка ушла на 283 получателя и дала Sent=0 / Errors=250 —
всю пачку сожгло системно битое сообщение (невалидная HTML-разметка или мёртвый
media file_id), которое провалилось бы на ПЕРВОЙ же отправке. Пре-флайт делает
одну реальную тестовую отправку в служебный чат (тот же, что и загрузка медиа) и,
если сообщение битое, падает с типизированной ошибкой ДО создания записи рассылки —
кабинет превращает её в 422 с машинно-читаемым reason, который можно показать
администратору.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from aiogram.exceptions import TelegramBadRequest
from fastapi import HTTPException, status

from app.config import settings
from app.services.broadcast_service import VALID_MEDIA_TYPES


if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup

    from app.services.broadcast_service import BroadcastMediaConfig


logger = structlog.get_logger(__name__)


# Стабильные reason-ключи (уходят в кабинет как {'reason': ...}) — не переименовывать.
REASON_INVALID_HTML = 'invalid_html'
REASON_INVALID_MEDIA = 'invalid_media'
REASON_PREFLIGHT_FAILED = 'preflight_failed'

_DETAIL_MAX = 200

# Подстроки ответа Telegram, различающие класс сбоя тестовой отправки.
_PARSE_ENTITIES_MARKER = "can't parse entities"
_MEDIA_ERROR_MARKERS = ('wrong file identifier', 'file reference', 'wrong remote file')


class BroadcastPreflightError(Exception):
    """Пре-флайт рассылки провалился: сообщение системно битое (битый HTML / мёртвый file_id)."""

    def __init__(self, reason_key: str, detail: str = '') -> None:
        self.reason_key = reason_key
        self.detail = detail
        super().__init__(f'{reason_key}: {detail}' if detail else reason_key)


def resolve_service_chat_id() -> int:
    """Служебный чат для тестовых отправок: канал уведомлений или первый админ.

    Единый резолвер для загрузки медиа (app/cabinet/routes/media.py) и пре-флайта
    рассылки — единственный источник правды, дублировать нельзя.
    """
    chat_id = settings.get_admin_notifications_chat_id()
    if chat_id is not None:
        return chat_id

    admin_ids = settings.get_admin_ids()
    if admin_ids:
        return admin_ids[0]

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail='No chat configured for file uploads',
    )


def _sanitize_detail(text: str) -> str:
    """Схлопывает пробелы и режет до _DETAIL_MAX символов (детали уходят в UI)."""
    return ' '.join(text.split())[:_DETAIL_MAX]


def _classify_preflight_bad_request(exc: TelegramBadRequest) -> BroadcastPreflightError:
    """Разбирает TelegramBadRequest тестовой отправки в стабильный reason."""
    detail = _sanitize_detail(str(exc))
    err = str(exc).lower()
    if _PARSE_ENTITIES_MARKER in err:
        return BroadcastPreflightError(REASON_INVALID_HTML, detail)
    if any(marker in err for marker in _MEDIA_ERROR_MARKERS):
        return BroadcastPreflightError(REASON_INVALID_MEDIA, detail)
    return BroadcastPreflightError(REASON_PREFLIGHT_FAILED, detail)


async def preflight_broadcast_message(
    bot: Bot,
    *,
    message_text: str,
    media: BroadcastMediaConfig | None = None,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    """Делает одну реальную тестовую отправку в служебный чат и валидирует её.

    Повторяет ТОТ ЖЕ путь, что и `BroadcastService._deliver_message`: то же
    сопоставление send_photo/send_video/send_document, ту же композицию подписи
    (`media.caption or message_text`), тот же parse_mode='HTML' и ту же клавиатуру —
    иначе пре-флайт лгал бы. При успехе тестовое сообщение удаляется best-effort
    (file_id сохраняется после удаления). Ничего не возвращает; при системно битом
    сообщении бросает `BroadcastPreflightError`.
    """
    if not settings.BROADCAST_PREFLIGHT_ENABLED:
        return

    chat_id = resolve_service_chat_id()

    try:
        if media is not None and media.type in VALID_MEDIA_TYPES:
            caption = media.caption or message_text
            media_methods = {
                'photo': ('photo', bot.send_photo),
                'video': ('video', bot.send_video),
                'document': ('document', bot.send_document),
            }
            kwarg_name, send_method = media_methods[media.type]
            message = await send_method(
                chat_id=chat_id,
                **{kwarg_name: media.file_id},
                caption=caption,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
        else:
            message = await bot.send_message(
                chat_id=chat_id,
                text=message_text,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
    except TelegramBadRequest as exc:
        raise _classify_preflight_bad_request(exc) from exc
    except Exception as exc:
        # Любой иной сбой тестовой отправки означает, что рассылка не взлетит.
        raise BroadcastPreflightError(REASON_PREFLIGHT_FAILED, _sanitize_detail(str(exc))) from exc

    # Успех → чистим за собой. Провал удаления не критичен: file_id уже валиден.
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception:
        logger.debug('Пре-флайт: не удалось удалить тестовое сообщение', chat_id=chat_id)
