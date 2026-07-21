import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.routes.branding import BRANDING_NAME_KEY, THEME_COLORS_KEY
from app.cabinet.services import email_layout
from app.config import settings


@pytest.mark.asyncio
async def test_render_branded_email_uses_defaults_and_wordmark(monkeypatch: pytest.MonkeyPatch):
    async def get_setting(_db: AsyncSession, _key: str) -> str | None:
        return None

    monkeypatch.setattr(email_layout, 'get_setting_value', get_setting)
    monkeypatch.setattr(settings, 'SMTP_FROM_NAME', 'Default Service')
    db: AsyncSession = AsyncMock()

    rendered = await email_layout.render_branded_email(
        db,
        title='Добро пожаловать',
        body_html='<p>Ваш доступ готов.</p>',
    )

    assert 'Default Service' in rendered
    assert '<img ' not in rendered
    assert '#0a0f1a' in rendered
    assert '#0f172a' in rendered
    assert '{' not in rendered


@pytest.mark.asyncio
async def test_render_branded_email_uses_custom_theme_and_accent_gradient(monkeypatch: pytest.MonkeyPatch):
    values = {
        THEME_COLORS_KEY: json.dumps(
            {
                'accent': '#00cc44',
                'darkBackground': '#030305',
                'darkSurface': '#0a1710',
                'darkText': '#f4fff7',
                'darkTextSecondary': '#a0b8a7',
            }
        ),
        BRANDING_NAME_KEY: 'Custom VPN',
    }

    async def get_setting(_db: AsyncSession, key: str) -> str | None:
        return values.get(key)

    monkeypatch.setattr(email_layout, 'get_setting_value', get_setting)
    db: AsyncSession = AsyncMock()

    rendered = await email_layout.render_branded_email(
        db,
        title='Доступ активирован',
        body_html='<p>Подключайтесь.</p>',
        cta_text='Открыть кабинет',
        cta_url='https://cabinet.example',
    )

    assert 'Custom VPN' in rendered
    assert '#030305' in rendered
    assert '#0a1710' in rendered
    assert 'linear-gradient(90deg, #00842c 0%, #00cc44 100%)' in rendered
    assert '#00511b' in rendered
    assert '{' not in rendered


@pytest.mark.asyncio
async def test_render_branded_email_uses_configured_header_image(monkeypatch: pytest.MonkeyPatch):
    values = {
        BRANDING_NAME_KEY: 'Custom VPN',
        email_layout.EMAIL_HEADER_URL_KEY: 'https://cdn.example/header.png',
    }

    async def get_setting(_db: AsyncSession, key: str) -> str | None:
        return values.get(key)

    monkeypatch.setattr(email_layout, 'get_setting_value', get_setting)
    db: AsyncSession = AsyncMock()

    rendered = await email_layout.render_branded_email(
        db,
        title='Новости',
        body_html='<p>Новый релиз.</p>',
    )

    assert '<img src="https://cdn.example/header.png"' in rendered
    assert 'alt="Custom VPN"' in rendered
    assert '{' not in rendered
