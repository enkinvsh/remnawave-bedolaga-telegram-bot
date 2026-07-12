"""Чистые render/validation-хелперы channel-post (без БД, без бота).

Проверяем канонизацию destination, сборку URL-клавиатуры и валидацию контента.
Все ошибки — подкласс ValueError со стабильным кодом (константы модуля).
"""

import pytest

from app.cabinet.schemas.channel_posts import ChannelPostButton, ChannelPostMedia
from app.services import channel_post_render as render


# ── canonicalize_destination ──────────────────────────────────────────────


def test_canonicalize_valid_negative_id() -> None:
    int_value, canonical = render.canonicalize_destination('-1001234567890')
    assert int_value == -1001234567890
    assert canonical == '-1001234567890'


def test_canonicalize_strips_whitespace() -> None:
    int_value, canonical = render.canonicalize_destination('  -100  ')
    assert int_value == -100
    assert canonical == '-100'


def test_canonicalize_zero_and_negative_zero_normalize() -> None:
    assert render.canonicalize_destination('0') == (0, '0')
    assert render.canonicalize_destination('-0') == (0, '0')


def test_canonicalize_rejects_username() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.canonicalize_destination('@name')
    assert exc.value.code == render.ERROR_BAD_DESTINATION


def test_canonicalize_rejects_non_numeric() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.canonicalize_destination('abc')
    assert exc.value.code == render.ERROR_BAD_DESTINATION


def test_canonicalize_rejects_empty() -> None:
    with pytest.raises(render.ChannelPostRenderError):
        render.canonicalize_destination('   ')


def test_canonicalize_rejects_underscore_grouping() -> None:
    with pytest.raises(render.ChannelPostRenderError):
        render.canonicalize_destination('1_000')


def test_canonicalize_rejects_overflow_above_int64() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.canonicalize_destination('999999999999999999999999')
    assert exc.value.code == render.ERROR_BAD_DESTINATION


def test_canonicalize_accepts_int64_boundaries() -> None:
    assert render.canonicalize_destination(str(render.INT64_MAX))[0] == render.INT64_MAX
    assert render.canonicalize_destination(str(render.INT64_MIN))[0] == render.INT64_MIN


def test_canonicalize_rejects_below_int64() -> None:
    with pytest.raises(render.ChannelPostRenderError):
        render.canonicalize_destination(str(render.INT64_MIN - 1))


# ── build_url_keyboard ────────────────────────────────────────────────────


def test_build_url_keyboard_empty_returns_none() -> None:
    assert render.build_url_keyboard([]) is None


def test_build_url_keyboard_shape_one_button_per_row() -> None:
    buttons = [
        ChannelPostButton(label='A', url='https://a.io'),
        ChannelPostButton(label='B', url='http://b.io'),
    ]
    markup = render.build_url_keyboard(buttons)
    assert markup is not None
    assert len(markup.inline_keyboard) == 2
    assert all(len(row) == 1 for row in markup.inline_keyboard)
    first = markup.inline_keyboard[0][0]
    assert first.text == 'A'
    assert first.url == 'https://a.io'
    assert first.callback_data is None


def test_build_url_keyboard_rejects_ftp_scheme() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.build_url_keyboard([ChannelPostButton(label='x', url='ftp://x.io')])
    assert exc.value.code == render.ERROR_BAD_BUTTON_URL


def test_build_url_keyboard_rejects_javascript_scheme() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.build_url_keyboard([ChannelPostButton(label='x', url='javascript:alert(1)')])
    assert exc.value.code == render.ERROR_BAD_BUTTON_URL


def test_build_url_keyboard_rejects_empty_label() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.build_url_keyboard([ChannelPostButton(label='   ', url='https://x.io')])
    assert exc.value.code == render.ERROR_BAD_BUTTON_LABEL


def test_build_url_keyboard_rejects_label_over_64() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.build_url_keyboard([ChannelPostButton(label='x' * 65, url='https://x.io')])
    assert exc.value.code == render.ERROR_BAD_BUTTON_LABEL


def test_build_url_keyboard_accepts_label_exactly_64() -> None:
    markup = render.build_url_keyboard([ChannelPostButton(label='x' * 64, url='https://x.io')])
    assert markup is not None


def test_build_url_keyboard_rejects_too_many_buttons() -> None:
    buttons = [ChannelPostButton(label=f'b{i}', url='https://x.io') for i in range(render.MAX_BUTTONS + 1)]
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.build_url_keyboard(buttons)
    assert exc.value.code == render.ERROR_TOO_MANY_BUTTONS


# ── validate_post_content ─────────────────────────────────────────────────


def test_validate_rejects_fully_empty() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_post_content(None, None)
    assert exc.value.code == render.ERROR_EMPTY_POST


def test_validate_rejects_empty_string_no_media() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_post_content('', None)
    assert exc.value.code == render.ERROR_EMPTY_POST


def test_validate_text_only_at_limit_ok() -> None:
    assert render.validate_post_content('x' * render.MAX_MESSAGE_LENGTH, None) is None


def test_validate_text_only_over_limit_rejected() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_post_content('x' * (render.MAX_MESSAGE_LENGTH + 1), None)
    assert exc.value.code == render.ERROR_TEXT_TOO_LONG


def test_validate_media_only_ok() -> None:
    media = ChannelPostMedia(type='photo', file_id='F')
    assert render.validate_post_content(None, media) is None


def test_validate_caption_at_limit_ok() -> None:
    media = ChannelPostMedia(type='video', file_id='F')
    assert render.validate_post_content('x' * render.MAX_CAPTION_LENGTH, media) is None


def test_validate_caption_over_limit_rejected() -> None:
    media = ChannelPostMedia(type='document', file_id='F')
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_post_content('x' * (render.MAX_CAPTION_LENGTH + 1), media)
    assert exc.value.code == render.ERROR_CAPTION_TOO_LONG
