"""Чистые render/validation-хелперы для channel-post.

БЕЗ БД, БЕЗ бота, БЕЗ импортов из broadcast_service — только детерминированные
функции над входными данными. Логика раскладки строк (одна URL-кнопка на строку)
зеркалит поведение create_broadcast_keyboard (handlers/admin/messages.py), но
реализована здесь заново, чтобы модуль оставался чистым.

Все ошибки — ``ChannelPostRenderError`` (подкласс ValueError) со стабильным
кодом-константой; никаких инлайн-строк.
"""

from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.cabinet.schemas.channel_posts import ChannelPostButton, ChannelPostMedia


# ── Стабильные коды ошибок (константы, не инлайн-литералы) ─────────────────
ERROR_BAD_DESTINATION = 'bad_destination'
ERROR_EMPTY_POST = 'empty_post'
ERROR_TEXT_TOO_LONG = 'text_too_long'
ERROR_CAPTION_TOO_LONG = 'caption_too_long'
ERROR_BAD_BUTTON_URL = 'bad_button_url'
ERROR_BAD_BUTTON_LABEL = 'bad_button_label'
ERROR_TOO_MANY_BUTTONS = 'too_many_buttons'

# ── Лимиты (Telegram) ─────────────────────────────────────────────────────
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
MAX_BUTTON_LABEL_LENGTH = 64
MAX_BUTTONS = 10

# ── Диапазон знакового 64-битного целого (Telegram chat id) ───────────────
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

_ALLOWED_URL_SCHEMES = ('http', 'https')


class ChannelPostRenderError(ValueError):
    """Ошибка валидации/рендеринга channel-post со стабильным кодом."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def canonicalize_destination(destination_id: str) -> tuple[int, str]:
    """Разобрать destination в (int, канонная десятичная строка).

    Принимает только опциональный знак и цифры (пробелы по краям обрезаются).
    Отвергает @username, нечисловое, разрядные подчёркивания и выход за границы
    знакового 64-битного диапазона. '-0' и '0' нормализуются в (0, '0').
    """
    raw = (destination_id or '').strip()
    body = raw[1:] if raw[:1] == '-' else raw
    if not body or not body.isdigit():
        raise ChannelPostRenderError(ERROR_BAD_DESTINATION, 'destination must be a numeric id')

    int_value = int(raw)
    if int_value < INT64_MIN or int_value > INT64_MAX:
        raise ChannelPostRenderError(ERROR_BAD_DESTINATION, 'destination out of int64 range')

    return int_value, str(int_value)


def build_url_keyboard(buttons: list[ChannelPostButton]) -> InlineKeyboardMarkup | None:
    """Собрать InlineKeyboardMarkup из URL-кнопок (по одной на строку).

    None при пустом списке. Проверяет http/https-схему, непустой label ≤64 и
    общий лимит кнопок.
    """
    if not buttons:
        return None
    if len(buttons) > MAX_BUTTONS:
        raise ChannelPostRenderError(ERROR_TOO_MANY_BUTTONS, 'too many buttons')

    rows: list[list[InlineKeyboardButton]] = []
    for btn in buttons:
        label = btn.label
        if not label.strip() or len(label) > MAX_BUTTON_LABEL_LENGTH:
            raise ChannelPostRenderError(ERROR_BAD_BUTTON_LABEL, 'invalid button label')
        if urlsplit(btn.url).scheme not in _ALLOWED_URL_SCHEMES:
            raise ChannelPostRenderError(ERROR_BAD_BUTTON_URL, 'button url must be http/https')
        rows.append([InlineKeyboardButton(text=label, url=btn.url)])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def validate_post_content(message_text: str | None, media: ChannelPostMedia | None) -> None:
    """Проверить содержимое поста. Возвращает None при валидности, иначе бросает.

    Пусто (нет текста И нет media) → ERROR_EMPTY_POST. С media текст = caption
    (≤1024), без media — только текст (≤4096).
    """
    if media is not None:
        if message_text and len(message_text) > MAX_CAPTION_LENGTH:
            raise ChannelPostRenderError(ERROR_CAPTION_TOO_LONG, 'caption too long')
        return

    if not message_text:
        raise ChannelPostRenderError(ERROR_EMPTY_POST, 'post must have text or media')
    if len(message_text) > MAX_MESSAGE_LENGTH:
        raise ChannelPostRenderError(ERROR_TEXT_TOO_LONG, 'message too long')
