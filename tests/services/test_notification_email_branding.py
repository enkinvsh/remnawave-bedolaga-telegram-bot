import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.services import email_layout
from app.cabinet.services.email_templates import EmailNotificationTemplates
from app.config import settings
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
    service._email_templates.get_content_only_html.return_value = body_html
    return service


def _user(language: str = 'ru') -> User:
    return User(
        id=1,
        email='user@example.com',
        email_verified=True,
        language=language,
        first_name='Иван',
        username='ivan',
    )


async def _render_default_balance_topup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    language: str,
    layout_enabled: bool,
) -> str:
    monkeypatch.setattr(settings, 'SMTP_FROM_NAME', 'Example Service')
    monkeypatch.setattr(settings, 'CABINET_URL', 'https://cabinet.example')
    service = NotificationDeliveryService()
    service._email_service = MagicMock()
    service._email_service.is_configured.return_value = True
    service._email_templates = EmailNotificationTemplates()
    db: AsyncSession = AsyncMock()
    deliver = AsyncMock(return_value=True)

    async def no_override(*_args, **_kwargs):
        return None

    async def get_setting(_db: AsyncSession, key: str) -> str | None:
        return str(layout_enabled).lower() if key == email_layout.EMAIL_LAYOUT_ENABLED_KEY else None

    monkeypatch.setattr(notification_delivery_service, 'AsyncSessionLocal', lambda: SessionContext(db))
    monkeypatch.setattr(notification_delivery_service, 'get_rendered_override', no_override)
    monkeypatch.setattr(notification_delivery_service, 'deliver_email', deliver)
    monkeypatch.setattr(email_layout, 'get_setting_value', get_setting)

    sent = await service._send_email_notification(
        _user(language),
        NotificationType.BALANCE_TOPUP,
        {'formatted_amount': '100.00 RUB', 'formatted_balance': '250.00 RUB'},
    )

    assert sent is True
    delivery_call = deliver.await_args
    assert delivery_call is not None
    return delivery_call.kwargs['html']


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


@pytest.mark.asyncio
async def test_balance_topup_appends_referral_block_only_inside_enabled_layout(monkeypatch: pytest.MonkeyPatch):
    original_html = '<p>Баланс пополнен</p>'
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
    monkeypatch.setattr(email_layout, 'build_referral_block', lambda *_args: '<section>referral</section>')

    sent = await service._send_email_notification(_user(), NotificationType.BALANCE_TOPUP, {})

    assert sent is True
    delivery_call = deliver.await_args
    assert delivery_call is not None
    assert '<section>referral</section>' in delivery_call.kwargs['html']


@pytest.mark.parametrize(
    ('language', 'expected_hash'),
    [
        ('ru', '6bd2e17f4dd0b4ff418c50166d0b455ac1405f23f93c011a48b9752ae5faf285'),
        ('en', '449f6ae0052f78d5a03108114716245470c5f71bc6279c1dcd908ff06627d4bd'),
        ('zh', '4ed7a5c5b3c78fd187069fdff623d3a88d24d8107bdb3174b15f690a8500ac27'),
        ('ua', 'bc497898a2211b6c68f10bc56d280a308944a54c27a58e78fca0d46f023c54c8'),
    ],
)
@pytest.mark.asyncio
async def test_default_balance_topup_html_is_byte_identical_when_layout_disabled(
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    expected_hash: str,
) -> None:
    body_html = await _render_default_balance_topup(
        monkeypatch,
        language=language,
        layout_enabled=False,
    )

    assert hashlib.sha256(body_html.encode()).hexdigest() == expected_hash


@pytest.mark.asyncio
async def test_default_balance_topup_uses_only_branded_chrome_when_layout_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body_html = await _render_default_balance_topup(
        monkeypatch,
        language='ru',
        layout_enabled=True,
    )

    assert '<h1>Example Service</h1>' not in body_html
    assert '&copy;' not in body_html
    assert '<h2>Баланс успешно пополнен!</h2>' not in body_html
    assert 'Спасибо за использование нашего сервиса!' not in body_html
    assert 'Сумма пополнения:' in body_html
    assert 'Текущий баланс:' in body_html
    assert 'class="button"' not in body_html
    assert body_html.count('background-image:linear-gradient(90deg') == 1
    tracked_href = (
        'href="https://cabinet.example?campaign=email_service&amp;utm_source=email'
        '&amp;utm_medium=email&amp;utm_campaign=email_service"'
    )
    assert body_html.count(tracked_href) == 1
