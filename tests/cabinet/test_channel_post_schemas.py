"""Строгие request-схемы channel-post: extra='forbid' — контракт безопасности.

Любое лишнее поле (category, selected_buttons, callback, media.caption) обязано
поднимать ValidationError, чтобы на уровне роутера превратиться в 422.
"""

import pytest
from pydantic import ValidationError

from app.cabinet.schemas.channel_posts import (
    ChannelPostButton,
    ChannelPostMedia,
    ChannelPostRequest,
)


def test_valid_request_parses() -> None:
    req = ChannelPostRequest(
        destination_id='-1001234567890',
        message_text='<b>hi</b>',
        custom_buttons=[ChannelPostButton(label='Open', url='https://example.com')],
        media=ChannelPostMedia(type='photo', file_id='FILEID123'),
    )
    assert req.destination_id == '-1001234567890'
    assert req.disable_web_page_preview is True
    assert req.custom_buttons[0].url == 'https://example.com'
    assert req.media is not None and req.media.type == 'photo'


def test_minimal_request_defaults() -> None:
    req = ChannelPostRequest(destination_id='-100', message_text='hello')
    assert req.custom_buttons == []
    assert req.media is None
    assert req.disable_web_page_preview is True


def test_button_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ChannelPostButton(label='Open', url='https://x.io', callback_data='oops')


def test_media_extra_caption_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ChannelPostMedia(type='photo', file_id='F', caption='nope')


def test_media_type_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        ChannelPostMedia(type='sticker', file_id='F')


def test_request_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ChannelPostRequest(destination_id='-100', message_text='x', category='promo')


def test_request_selected_buttons_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ChannelPostRequest(destination_id='-100', message_text='x', selected_buttons=['home'])


def test_message_thread_id_defaults_none() -> None:
    req = ChannelPostRequest(destination_id='-100', message_text='x')
    assert req.message_thread_id is None


def test_message_thread_id_accepts_positive() -> None:
    req = ChannelPostRequest(destination_id='-100', message_text='x', message_thread_id=5)
    assert req.message_thread_id == 5


def test_message_thread_id_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        ChannelPostRequest(destination_id='-100', message_text='x', message_thread_id=0)


def test_message_thread_id_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        ChannelPostRequest(destination_id='-100', message_text='x', message_thread_id=-1)
