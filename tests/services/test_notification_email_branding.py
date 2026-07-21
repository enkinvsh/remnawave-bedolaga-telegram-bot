from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.services import email_layout
from app.database.models import User
from app.services import notification_delivery_service
from app.services.notification_delivery_service import NotificationDeliveryService, NotificationType


class SessionContext:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def __aenter__(self) -> AsyncSession:
        return self.db

    async def __aexit__(self, *_exc) -> bool:
        return False


def _service_with_template(body_html: str) -> NotificationDeliveryService:
    service = NotificationDeliveryService()
    service._email_service = MagicMock()
    service._email_service.is_configured.return_value = True
    service._email_templates = MagicMock()
    service._email_templates.get_template.return_value = {
        'subject': 'Тема письма',
        'body_html': body_html,
        'body_text': 'Текст письма',
    }
    return service


def _user() -> User:
    return User(
        id=1,
        email='user@example.com',
        email_verified=True,
        language='ru',
        first_name='Иван',
        username='ivan',
    )


@pytest.mark.asyncio
async def test_sender_preserves_original_html_when_layout_disabled(monkeypatch: pytest.MonkeyPatch):
    original_html = '<p>Исходное письмо</p>'
    service = _service_with_template(original_html)
    db: AsyncSession = AsyncMock()
    deliver = AsyncMock(return_value=True)

    async def no_override(*_args, **_kwargs):
        return None

    async def get_setting(_db: AsyncSession, key: str) -> str | None:
        return 'false' if key == email_layout.EMAIL_LAYOUT_ENABLED_KEY else None

    monkeypatch.setattr(notification_delivery_service, 'AsyncSessionLocal', lambda: SessionContext(db))
    monkeypatch.setattr(notification_delivery_service, 'get_rendered_override', no_override)
    monkeypatch.setattr(notification_delivery_service, 'deliver_email', deliver)
    monkeypatch.setattr(email_layout, 'get_setting_value', get_setting)

    sent = await service._send_email_notification(_user(), NotificationType.EMAIL_VERIFICATION, {})

    assert sent is True
    deliver.assert_awaited_once()
    delivery_call = deliver.await_args
    assert delivery_call is not None
    assert delivery_call.kwargs['html'] == original_html


@pytest.mark.asyncio
async def test_sender_wraps_html_when_layout_enabled(monkeypatch: pytest.MonkeyPatch):
    original_html = '<p>Исходное письмо</p>'
    service = _service_with_template(original_html)
    db: AsyncSession = AsyncMock()
    deliver = AsyncMock(return_value=True)

    async def no_override(*_args, **_kwargs):
        return None

    async def get_setting(_db: AsyncSession, key: str) -> str | None:
        return 'true' if key == email_layout.EMAIL_LAYOUT_ENABLED_KEY else None

    monkeypatch.setattr(notification_delivery_service, 'AsyncSessionLocal', lambda: SessionContext(db))
    monkeypatch.setattr(notification_delivery_service, 'get_rendered_override', no_override)
    monkeypatch.setattr(notification_delivery_service, 'deliver_email', deliver)
    monkeypatch.setattr(email_layout, 'get_setting_value', get_setting)

    sent = await service._send_email_notification(_user(), NotificationType.EMAIL_VERIFICATION, {})

    assert sent is True
    deliver.assert_awaited_once()
    delivery_call = deliver.await_args
    assert delivery_call is not None
    wrapped = delivery_call.kwargs['html']
    assert wrapped != original_html
    assert original_html in wrapped
    assert wrapped.lower().count('<!doctype') == 1
