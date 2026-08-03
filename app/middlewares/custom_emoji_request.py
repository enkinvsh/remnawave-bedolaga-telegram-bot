"""Session request-middleware: подстановка кастомных эмодзи в исходящие сообщения."""

import time
from typing import Any, Final

import structlog
from aiogram import Bot
from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.enums import ParseMode
from aiogram.methods import (
    EditMessageCaption,
    EditMessageText,
    Response,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMediaGroup,
    SendMessage,
    SendPhoto,
    SendVideo,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup

from app.config import settings
from app.utils.custom_emoji import get_leading_emoji_id, substitute_custom_emoji
from app.utils.miniapp_buttons import strip_leading_emoji


logger = structlog.get_logger(__name__)

#: Положительный allowlist: пара «класс метода -> поле с текстом». Всё остальное — сквозняком.
TEXT_FIELDS: Final[dict[type[TelegramMethod[Any]], str]] = {
    SendMessage: 'text',
    EditMessageText: 'text',
    SendPhoto: 'caption',
    SendVideo: 'caption',
    SendDocument: 'caption',
    SendAudio: 'caption',
    SendAnimation: 'caption',
    EditMessageCaption: 'caption',
}

#: Разметки с кнопками: класс -> поле со списком рядов. ReplyKeyboardRemove/ForceReply сюда не входят.
MARKUP_ROWS: Final[dict[type[Any], str]] = {
    InlineKeyboardMarkup: 'inline_keyboard',
    ReplyKeyboardMarkup: 'keyboard',
}

_ENTITY_FIELDS: Final[tuple[str, ...]] = ('entities', 'caption_entities')
_LOG_INTERVAL_SECONDS: Final[float] = 60.0

_last_failure_log: float = 0.0


def _log_transform_failure(error: Exception) -> None:
    """Записать предупреждение не чаще раза в минуту, чтобы не залить логи."""
    global _last_failure_log
    now = time.monotonic()
    if _last_failure_log and now - _last_failure_log < _LOG_INTERVAL_SECONDS:
        return
    _last_failure_log = now
    logger.warning('Не удалось подставить кастомные эмодзи', error=str(error), error_type=type(error).__name__)


def _resolve_parse_mode(bot: Bot, value: Any) -> Any:
    """Развернуть Default-сентинел: на этом слое parse_mode ещё НЕ резолвнут aiogram'ом."""
    if isinstance(value, Default):
        defaults = getattr(bot, 'default', None)
        if defaults is None:
            return None
        return defaults[value.name]
    return value


def _is_html(bot: Bot, value: Any) -> bool:
    resolved = _resolve_parse_mode(bot, value)
    return isinstance(resolved, str) and resolved.lower() == ParseMode.HTML.value.lower()


def _chat_allowed(chat_id: Any) -> bool:
    """Канареечный режим: непустой список chat_id ограничивает подстановку этими чатами."""
    canary = settings.get_custom_emoji_test_chat_ids()
    if not canary:
        return True
    return isinstance(chat_id, int) and chat_id in canary


def _convert_button(button: Any) -> Any:
    """Перенести ведущий эмодзи лейбла в `icon_custom_emoji_id`. Кнопки не парсят HTML."""
    if getattr(button, 'icon_custom_emoji_id', None):
        # Иконку уже проставил человек в админке — ручная настройка всегда выигрывает.
        return button

    text = getattr(button, 'text', None)
    if not isinstance(text, str) or not text:
        return button

    emoji_id = get_leading_emoji_id(text)
    if emoji_id is None:
        return button

    stripped = strip_leading_emoji(text)
    if not stripped.strip():
        # Telegram отвергает пустой текст кнопки: лейбл из одного эмодзи не трогаем.
        return button

    return button.model_copy(update={'text': stripped, 'icon_custom_emoji_id': emoji_id})


class CustomEmojiRequestMiddleware(BaseRequestMiddleware):
    """Заменяет юникод-эмодзи на `<tg-emoji>` в тексте/подписи исходящих методов."""

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        prepared = method
        try:
            prepared = self._transform(bot, method)
        except Exception as error:  # падение middleware убило бы каждый запрос к API
            _log_transform_failure(error)
            prepared = method
        return await make_request(bot, prepared)

    def _transform(self, bot: Bot, method: TelegramMethod[TelegramType]) -> TelegramMethod[TelegramType]:
        if not settings.CUSTOM_EMOJI_ENABLED:
            return method
        if not _chat_allowed(getattr(method, 'chat_id', None)):
            return method
        if isinstance(method, SendMediaGroup):
            return self._transform_media_group(bot, method)

        # Две НЕЗАВИСИМЫЕ ветки: текст/подпись живут под HTML-гейтом, кнопки — нет.
        updates: dict[str, Any] = self._text_update(bot, method)
        markup = self._markup_update(method)
        if markup is not None:
            updates['reply_markup'] = markup

        if not updates:
            return method
        return method.model_copy(update=updates)

    def _text_update(self, bot: Bot, method: TelegramMethod[TelegramType]) -> dict[str, Any]:
        """Текстовая ветка: allowlist класс/поле, отсутствие entities и effective parse_mode == HTML."""
        field = TEXT_FIELDS.get(type(method))
        if field is None:
            return {}
        if any(getattr(method, name, None) is not None for name in _ENTITY_FIELDS):
            return {}
        if not _is_html(bot, getattr(method, 'parse_mode', None)):
            return {}

        value = getattr(method, field, None)
        if not isinstance(value, str) or not value:
            return {}

        updated = substitute_custom_emoji(value)
        if updated == value:
            return {}
        return {field: updated}

    def _markup_update(self, method: TelegramMethod[TelegramType]) -> Any:
        """Кнопочная ветка: любой метод с reply_markup, без оглядки на parse_mode и allowlist."""
        markup = getattr(method, 'reply_markup', None)
        rows_field = MARKUP_ROWS.get(type(markup))
        if rows_field is None:
            return None

        rows = getattr(markup, rows_field, None)
        if not rows:
            return None

        changed = False
        updated_rows: list[list[Any]] = []
        for row in rows:
            updated_row: list[Any] = []
            for button in row:
                converted = _convert_button(button)
                changed = changed or converted is not button
                updated_row.append(converted)
            updated_rows.append(updated_row)

        if not changed:
            return None
        return markup.model_copy(update={rows_field: updated_rows})

    def _transform_media_group(self, bot: Bot, method: SendMediaGroup) -> TelegramMethod[TelegramType]:
        """У SendMediaGroup нет внешнего parse_mode: он свой у каждого элемента media."""
        items: list[Any] = []
        changed = False
        for item in method.media:
            caption = getattr(item, 'caption', None)
            if (
                isinstance(caption, str)
                and caption
                and getattr(item, 'caption_entities', None) is None
                and _is_html(bot, getattr(item, 'parse_mode', None))
            ):
                updated = substitute_custom_emoji(caption)
                if updated != caption:
                    items.append(item.model_copy(update={'caption': updated}))
                    changed = True
                    continue
            items.append(item)

        if not changed:
            return method
        return method.model_copy(update={'media': items})
