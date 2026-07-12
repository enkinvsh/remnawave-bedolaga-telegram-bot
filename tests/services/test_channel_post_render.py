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


def test_build_url_keyboard_passes_style_and_icon() -> None:
    button = ChannelPostButton(label='A', url='https://a.io', style='primary', icon_custom_emoji_id='123456')
    markup = render.build_url_keyboard([button])
    assert markup is not None
    btn = markup.inline_keyboard[0][0]
    assert btn.style == 'primary'
    assert btn.icon_custom_emoji_id == '123456'


def test_build_url_keyboard_omits_absent_style_and_icon() -> None:
    markup = render.build_url_keyboard([ChannelPostButton(label='A', url='https://a.io')])
    assert markup is not None
    dumped = markup.inline_keyboard[0][0].model_dump(exclude_none=True)
    assert 'style' not in dumped
    assert 'icon_custom_emoji_id' not in dumped


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


# ── validate_rich_content (extended allowlist) ─────────────────────────────


ALLOWED_RICH_SAMPLES = [
    '<h2>Title</h2>',
    '<h4>Sub</h4>',
    '<p>Para</p>',
    '<details><summary>More</summary><p>hidden</p></details>',
    '<ul><li>a</li><li>b</li></ul>',
    '<ol><li>a</li></ol>',
    '<table bordered striped><tr><th>H</th></tr><tr><td>C</td></tr></table>',
    '<img src="https://cdn.example/x.png">',
    '<hr>',
    '<footer>foot</footer>',
    '<mark>hl</mark>',
    '<sub>lo</sub><sup>hi</sup>',
    '<tg-reference name="ref">r</tg-reference>',
    '<a href="https://a.io">link</a>',
    '<a href="#anchor">jump</a>',
    '<b>b</b><strong>s</strong><i>i</i><em>e</em>',
    '<u>u</u><ins>i</ins><s>s</s><strike>x</strike><del>d</del>',
    '<code>c</code><pre>p</pre><blockquote>q</blockquote>',
    '<tg-spoiler>sp</tg-spoiler>',
    '<tg-emoji emoji-id="123456">😀</tg-emoji>',
    '<span class="tg-spoiler">x</span>',
]


@pytest.mark.parametrize('sample', ALLOWED_RICH_SAMPLES)
def test_validate_rich_accepts_allowed(sample: str) -> None:
    assert render.validate_rich_content(sample) is None


def test_validate_rich_rejects_script() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<script>alert(1)</script>')
    assert exc.value.code == render.ERROR_RICH_BAD_TAG


def test_validate_rich_rejects_div() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<div>x</div>')
    assert exc.value.code == render.ERROR_RICH_BAD_TAG


def test_validate_rich_rejects_onclick_attr() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<p onclick="evil()">x</p>')
    assert exc.value.code == render.ERROR_RICH_BAD_ATTR


def test_validate_rich_rejects_img_non_https() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<img src="http://insecure/x.png">')
    assert exc.value.code == render.ERROR_RICH_BAD_ATTR


def test_validate_rich_rejects_over_limit() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('a' * (render.MAX_RICH_LENGTH + 1))
    assert exc.value.code == render.ERROR_RICH_TOO_LONG


def test_validate_rich_accepts_at_limit() -> None:
    assert render.validate_rich_content('a' * render.MAX_RICH_LENGTH) is None


def test_validate_rich_rejects_empty() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('')
    assert exc.value.code == render.ERROR_EMPTY_POST


def test_validate_rich_accepts_br() -> None:
    assert render.validate_rich_content('<p>a<br>b</p>') is None


def test_validate_rich_accepts_br_self_closing() -> None:
    assert render.validate_rich_content('<p>a<br/>b</p>') is None


def test_validate_rich_bad_tag_extra_carries_tag() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<div>x</div>')
    assert exc.value.extra == {'tag': 'div'}


def test_validate_rich_bad_attr_extra_carries_tag_and_attr() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<p onclick="evil()">x</p>')
    assert exc.value.extra == {'tag': 'p', 'attr': 'onclick'}


def test_render_error_extra_defaults_empty() -> None:
    err = render.ChannelPostRenderError(render.ERROR_EMPTY_POST)
    assert err.extra == {}


# ── v8: media blocks (video/audio/figure/collage/slideshow/map) ────────────


ALLOWED_V8_SAMPLES = [
    '<video src="https://x/v.mp4"></video>',
    '<audio src="https://x/a.mp3"></audio>',
    '<tg-map lat="41.9" long="12.5" zoom="14"/>',
    '<video src="https://x/v.mp4" tg-spoiler></video>',
    '<img src="https://x/i.png" tg-spoiler/>',
    '<tg-collage><img src="https://x/i.png"/><video src="https://x/v.mp4"></video><figcaption>c</figcaption></tg-collage>',
    '<tg-slideshow><img src="https://x/i.png"/><video src="https://x/v.mp4"></video><figcaption>c</figcaption></tg-slideshow>',
    '<figure><img src="https://x/i.png" tg-spoiler/><figcaption>cap <cite>credit</cite></figcaption></figure>',
    '<tg-map lat="41.9" long="12.5" zoom="13"/>',
    '<tg-map lat="-0.5" long="180" zoom="20"/>',
]


@pytest.mark.parametrize('sample', ALLOWED_V8_SAMPLES)
def test_validate_rich_accepts_v8_media(sample: str) -> None:
    assert render.validate_rich_content(sample) is None


def test_validate_rich_rejects_video_http_src() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<video src="http://x/v.mp4"></video>')
    assert exc.value.code == render.ERROR_RICH_BAD_ATTR
    assert exc.value.extra == {'tag': 'video', 'attr': 'src'}


def test_validate_rich_rejects_audio_missing_src() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<audio></audio>')
    assert exc.value.code == render.ERROR_RICH_BAD_ATTR
    assert exc.value.extra == {'tag': 'audio', 'attr': 'src'}


def test_validate_rich_rejects_img_missing_src() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<img/>')
    assert exc.value.code == render.ERROR_RICH_BAD_ATTR
    assert exc.value.extra == {'tag': 'img', 'attr': 'src'}


def test_validate_rich_rejects_map_zoom_below_range() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<tg-map lat="41.9" long="12.5" zoom="12"/>')
    assert exc.value.code == render.ERROR_RICH_BAD_ATTR
    assert exc.value.extra == {'tag': 'tg-map', 'attr': 'zoom'}


def test_validate_rich_rejects_map_zoom_above_range() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<tg-map lat="41.9" long="12.5" zoom="21"/>')
    assert exc.value.code == render.ERROR_RICH_BAD_ATTR
    assert exc.value.extra == {'tag': 'tg-map', 'attr': 'zoom'}


def test_validate_rich_rejects_map_lat_not_float() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<tg-map lat="abc" long="12.5" zoom="14"/>')
    assert exc.value.code == render.ERROR_RICH_BAD_ATTR
    assert exc.value.extra == {'tag': 'tg-map', 'attr': 'lat'}


def test_validate_rich_rejects_unknown_attr_on_collage() -> None:
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content('<tg-collage cols="3"><img src="https://x/i.png"/></tg-collage>')
    assert exc.value.code == render.ERROR_RICH_BAD_ATTR
    assert exc.value.extra == {'tag': 'tg-collage', 'attr': 'cols'}


def test_validate_rich_accepts_exactly_50_media() -> None:
    html = '<img src="https://x/i.png"/>' * render.MAX_RICH_MEDIA
    assert render.validate_rich_content(html) is None


def test_validate_rich_rejects_51_media() -> None:
    html = '<img src="https://x/i.png"/>' * (render.MAX_RICH_MEDIA + 1)
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content(html)
    assert exc.value.code == render.ERROR_RICH_TOO_MANY_MEDIA


def test_media_count_mixes_img_video_audio() -> None:
    unit = '<img src="https://x/i.png"/><video src="https://x/v.mp4"></video><audio src="https://x/a.mp3"></audio>'
    # 17 units = 51 media tags → over the 50 cap.
    with pytest.raises(render.ChannelPostRenderError) as exc:
        render.validate_rich_content(unit * 17)
    assert exc.value.code == render.ERROR_RICH_TOO_MANY_MEDIA
