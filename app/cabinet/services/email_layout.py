import re
from html import escape
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.services.email_referral_block import build_referral_block
from app.config import settings
from app.database.crud.system_setting import get_setting_value
from app.templates.email.fragments import NEUTRAL_DEFAULTS, cta_block, header_image_block, wordmark_block


EMAIL_HEADER_URL_KEY: Final = 'CABINET_EMAIL_HEADER_URL'
EMAIL_LAYOUT_ENABLED_KEY: Final = 'CABINET_EMAIL_LAYOUT_ENABLED'
EMAIL_SERVICE_CAMPAIGN: Final = 'email_service'
EMAIL_SERVICE_CTA_TEXT: Final = 'Открыть личный кабинет'
BRANDING_NAME_KEY: Final = 'CABINET_BRANDING_NAME'
THEME_COLORS_KEY: Final = 'CABINET_THEME_COLORS'
DEFAULT_EMAIL_THEME_COLORS: Final = {
    'accent': '#3b82f6',
    'darkBackground': '#0a0f1a',
    'darkSurface': '#0f172a',
    'darkText': '#f1f5f9',
    'darkTextSecondary': '#94a3b8',
}
BASE_TEMPLATE: Final = (Path(__file__).parents[2] / 'templates' / 'email' / 'base.html').read_text(encoding='utf-8')
THEME_COLORS_ADAPTER: Final = TypeAdapter(dict[str, str])
BODY_PATTERN: Final = re.compile(r'<body\b[^>]*>(?P<body>.*)</body>', re.IGNORECASE | re.DOTALL)


def email_service_cta_url() -> str:
    cabinet_url = settings.CABINET_URL.rstrip('/')
    return (
        f'{cabinet_url}?campaign={EMAIL_SERVICE_CAMPAIGN}'
        f'&utm_source=email&utm_medium=email&utm_campaign={EMAIL_SERVICE_CAMPAIGN}'
    )


def _normalize_hex(color: str) -> str:
    value = color.removeprefix('#')
    if len(value) == 3:
        value = ''.join(channel * 2 for channel in value)
    return value


def _darken_hex(color: str, amount: float) -> str:
    value = _normalize_hex(color)
    channels = (int(value[index : index + 2], 16) for index in range(0, 6, 2))
    return '#' + ''.join(f'{int(channel * (1 - amount)):02x}' for channel in channels)


def _lighten_hex(color: str, amount: float) -> str:
    value = _normalize_hex(color)
    channels = (int(value[index : index + 2], 16) for index in range(0, 6, 2))
    return '#' + ''.join(f'{int(channel + (255 - channel) * amount):02x}' for channel in channels)


async def _theme_colors(db: AsyncSession) -> dict[str, str]:
    colors = DEFAULT_EMAIL_THEME_COLORS.copy()
    colors_json = await get_setting_value(db, THEME_COLORS_KEY)
    if colors_json:
        parsed = THEME_COLORS_ADAPTER.validate_json(colors_json)
        colors.update(parsed)
    return colors


async def render_branded_email(
    db: AsyncSession,
    *,
    title: str,
    body_html: str,
    cta_text: str | None = None,
    cta_url: str | None = None,
    footer_note: str = '',
    unsubscribe_html: str = '',
    preheader: str = '',
) -> str:
    colors = await _theme_colors(db)
    accent = colors['accent']
    text_color = colors['darkText']
    dim_color = colors['darkTextSecondary']
    card_radius = NEUTRAL_DEFAULTS['card_radius']
    service_name = await get_setting_value(db, BRANDING_NAME_KEY) or settings.SMTP_FROM_NAME or 'VPN Service'
    header_url = await get_setting_value(db, EMAIL_HEADER_URL_KEY)

    header_block = (
        header_image_block(escape(header_url, quote=True), escape(service_name), card_radius)
        if header_url
        else wordmark_block(escape(service_name), text_color, card_radius)
    )
    rendered_cta = ''
    if cta_text and cta_url:
        rendered_cta = cta_block(
            escape(cta_text),
            escape(cta_url, quote=True),
            _darken_hex(accent, 0.35),
            accent,
            _darken_hex(accent, 0.2),
            _lighten_hex(accent, 0.15),
            '#ffffff',
            NEUTRAL_DEFAULTS['button_radius'],
        )

    return BASE_TEMPLATE.format(
        subject=title,
        preheader=preheader,
        header_block=header_block,
        title=title,
        body_html=body_html,
        cta_block=rendered_cta,
        footer_note=footer_note,
        unsubscribe_block=unsubscribe_html,
        footer_address=escape(service_name),
        bg_color='transparent',
        card_color=colors['darkSurface'],
        card_border_color=_darken_hex(accent, 0.6),
        card_radius=card_radius,
        footer_border_color=_darken_hex(dim_color, 0.65),
        text_color=text_color,
        dim_color=dim_color,
        muted_color=_darken_hex(dim_color, 0.4),
        font_stack=NEUTRAL_DEFAULTS['font_stack'],
        title_font_stack=NEUTRAL_DEFAULTS['title_font_stack'],
    )


async def render_branded_email_if_enabled(
    db: AsyncSession,
    *,
    title: str,
    body_html: str,
    content_html: str | None = None,
    cta_text: str | None = None,
    cta_url: str | None = None,
    include_referral: bool = False,
) -> str:
    enabled = await get_setting_value(db, EMAIL_LAYOUT_ENABLED_KEY)
    if enabled is None or enabled.lower() != 'true':
        return body_html
    if content_html is None:
        body_match = BODY_PATTERN.search(body_html)
        body_content = body_match.group('body') if body_match else body_html
    else:
        body_content = content_html
    if include_referral:
        colors = await _theme_colors(db)
        body_content += build_referral_block(colors['darkText'], colors['darkTextSecondary'], colors['accent'])
    return await render_branded_email(
        db,
        title=title,
        body_html=body_content,
        cta_text=cta_text,
        cta_url=cta_url,
    )
