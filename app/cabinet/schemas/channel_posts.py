"""Строгие Pydantic v2 request-схемы для channel-post.

Единственный источник caption — ``message_text`` (у media НЕТ поля caption).
Кнопки только URL (никаких callback). ``extra='forbid'`` на всех трёх моделях —
контракт безопасности: любое лишнее поле (category, selected_buttons, callback,
media.caption) поднимает ValidationError и превращается в 422 на роутере.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChannelPostButton(BaseModel):
    """URL-кнопка (без callback)."""

    model_config = ConfigDict(extra='forbid')

    label: str
    url: str


class ChannelPostMedia(BaseModel):
    """Одиночное вложение. Caption не хранится здесь — им служит message_text."""

    model_config = ConfigDict(extra='forbid')

    type: Literal['photo', 'video', 'document']
    file_id: str


class ChannelPostRequest(BaseModel):
    """Запрос на публикацию одного сообщения в разрешённый канал/группу."""

    model_config = ConfigDict(extra='forbid')

    destination_id: str
    message_text: str | None = None
    custom_buttons: list[ChannelPostButton] = []
    media: ChannelPostMedia | None = None
    disable_web_page_preview: bool = True
    message_thread_id: int | None = Field(default=None, ge=1)
    rich: bool = False
