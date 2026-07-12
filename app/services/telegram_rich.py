"""Custom aiogram method for Bot API 10.1 ``sendRichMessage``.

aiogram 3.25 has no built-in method for it; ``TelegramMethod`` has
``model_config extra='allow'`` so a subclass routes through the configured
session/proxy via ``await bot(method)``. Importable without a bot instance.
"""

from __future__ import annotations

from aiogram.methods.base import TelegramMethod
from aiogram.types import InlineKeyboardMarkup, Message


class SendRichMessage(TelegramMethod[Message]):
    __api_method__ = 'sendRichMessage'
    __returning__ = Message

    chat_id: int | str
    message_thread_id: int | None = None
    rich_message: dict
    reply_markup: InlineKeyboardMarkup | None = None
