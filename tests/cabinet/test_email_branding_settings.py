from unittest.mock import ANY, AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.routes import branding
from app.database.models import User


@pytest.mark.asyncio
async def test_email_layout_setting_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch):
    async def get_setting(_db: AsyncSession, _key: str) -> str | None:
        return None

    monkeypatch.setattr(branding, 'get_setting_value', get_setting)

    response = await branding.get_email_layout_enabled(AsyncMock())

    assert response.enabled is False


@pytest.mark.asyncio
async def test_email_layout_setting_can_be_enabled(monkeypatch: pytest.MonkeyPatch):
    set_setting = AsyncMock()
    monkeypatch.setattr(branding, 'set_setting_value', set_setting)
    payload = branding.EmailLayoutEnabledUpdate(enabled=True)
    admin = User(id=1, telegram_id=123)

    response = await branding.update_email_layout_enabled(payload, admin=admin, db=AsyncMock())

    assert response.enabled is True
    set_setting.assert_awaited_once_with(ANY, branding.EMAIL_LAYOUT_ENABLED_KEY, 'true')
