"""Чистые render/validation-хелперы для channel-post.

БЕЗ БД, БЕЗ бота, БЕЗ импортов из broadcast_service — только детерминированные
функции над входными данными. Логика раскладки строк (одна URL-кнопка на строку)
зеркалит поведение create_broadcast_keyboard (handlers/admin/messages.py), но
реализована здесь заново, чтобы модуль оставался чистым.

Все ошибки — ``ChannelPostRenderError`` (подкласс ValueError) со стабильным
кодом-константой; никаких инлайн-строк.
"""

import re
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
ERROR_RICH_BAD_TAG = 'rich_bad_tag'
ERROR_RICH_BAD_ATTR = 'rich_bad_attr'
ERROR_RICH_TOO_LONG = 'rich_too_long'
ERROR_RICH_WITH_MEDIA = 'rich_with_media'

# ── Лимиты (Telegram) ─────────────────────────────────────────────────────
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
MAX_BUTTON_LABEL_LENGTH = 64
MAX_BUTTONS = 10
MAX_RICH_LENGTH = 32000

# ── Диапазон знакового 64-битного целого (Telegram chat id) ───────────────
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

_ALLOWED_URL_SCHEMES = ('http', 'https')

# ── Rich-режим (sendRichMessage): расширенный allowlist ────────────────────
# Теги без атрибутов не входят в RICH_ALLOWED_ATTRS (любой атрибут → reject).
RICH_ALLOWED_TAGS = frozenset(
    {
        'h2',
        'h4',
        'p',
        'details',
        'summary',
        'ul',
        'ol',
        'li',
        'table',
        'tr',
        'th',
        'td',
        'img',
        'hr',
        'br',
        'footer',
        'mark',
        'sub',
        'sup',
        'tg-reference',
        'a',
        'b',
        'strong',
        'i',
        'em',
        'u',
        'ins',
        's',
        'strike',
        'del',
        'code',
        'pre',
        'blockquote',
        'tg-spoiler',
        'tg-emoji',
        'span',
    }
)
RICH_ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    'img': frozenset({'src'}),
    'a': frozenset({'href'}),
    'tg-emoji': frozenset({'emoji-id'}),
    'tg-reference': frozenset({'name'}),
    'span': frozenset({'class'}),
    'table': frozenset({'bordered', 'striped'}),
}
_RICH_TAG_RE = re.compile(r'<(/?)([a-zA-Z][a-zA-Z0-9-]*)([^>]*)>')
_RICH_ATTR_RE = re.compile(r'([a-zA-Z][a-zA-Z0-9-]*)(?:\s*=\s*"([^"]*)"|\s*=\s*\'([^\']*)\')?')


class ChannelPostRenderError(ValueError):
    """Ошибка валидации/рендеринга channel-post со стабильным кодом.

    ``extra`` несёт санитизированные детали (только имена тега/атрибута, уже
    ограниченные regex) для диагностики на клиенте.
    """

    def __init__(self, code: str, message: str | None = None, extra: dict | None = None) -> None:
        self.code = code
        self.extra = extra or {}
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
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    url=btn.url,
                    style=btn.style,
                    icon_custom_emoji_id=btn.icon_custom_emoji_id,
                )
            ]
        )

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


def _validate_rich_attrs(tag_name: str, attrs_blob: str) -> None:
    allowed = RICH_ALLOWED_ATTRS.get(tag_name, frozenset())
    for match in _RICH_ATTR_RE.finditer(attrs_blob):
        attr_name = match.group(1).lower()
        if attr_name not in allowed:
            raise ChannelPostRenderError(
                ERROR_RICH_BAD_ATTR,
                f'attribute not allowed: {attr_name}',
                extra={'tag': tag_name, 'attr': attr_name},
            )
        if tag_name == 'img' and attr_name == 'src':
            src = match.group(2) if match.group(2) is not None else (match.group(3) or '')
            if urlsplit(src).scheme != 'https':
                raise ChannelPostRenderError(
                    ERROR_RICH_BAD_ATTR,
                    'img src must be https',
                    extra={'tag': tag_name, 'attr': attr_name},
                )


def validate_rich_content(html: str | None) -> None:
    """Проверить rich-HTML по расширенному allowlist. None при валидности.

    Пусто → ERROR_EMPTY_POST; >32000 → ERROR_RICH_TOO_LONG; неизвестный тег →
    ERROR_RICH_BAD_TAG; запрещённый атрибут или non-https img → ERROR_RICH_BAD_ATTR.
    """
    if not html:
        raise ChannelPostRenderError(ERROR_EMPTY_POST, 'rich post must have content')
    if len(html) > MAX_RICH_LENGTH:
        raise ChannelPostRenderError(ERROR_RICH_TOO_LONG, 'rich message too long')

    for is_closing, tag_name_raw, attrs_blob in _RICH_TAG_RE.findall(html):
        tag_name = tag_name_raw.lower()
        if tag_name not in RICH_ALLOWED_TAGS:
            raise ChannelPostRenderError(ERROR_RICH_BAD_TAG, f'tag not allowed: {tag_name}', extra={'tag': tag_name})
        if not is_closing:
            _validate_rich_attrs(tag_name, attrs_blob)
