"""MonitoringService._check_expired_subscriptions must cross-check the RemnaWave
panel before expiring + notifying a subscription.

Users with genuinely-active panel subscriptions were intermittently getting the
scary "⛔ Подписка истекла" message when the LOCAL end_date was transiently
corrupted (panel desync). The monitor now asks the panel: if the panel still says
ACTIVE with a future expiry, this is a FALSE expiry — heal the local end_date and
stay silent. Any uncertainty (panel error) defers the decision to the next cycle
without notifying. Genuinely-expired subs still expire + notify exactly as before.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.config import settings
from app.database.models import SubscriptionStatus
from app.external.remnawave_api import UserStatus as RemnaWaveUserStatus
from app.services import monitoring_service as ms
from app.services.monitoring_service import MonitoringService


def _db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    return db


def _sub(*, remnawave_uuid: str | None, user_uuid: str | None) -> SimpleNamespace:
    user = SimpleNamespace(id=7, telegram_id=123, remnawave_uuid=user_uuid)
    return SimpleNamespace(
        id=1,
        user_id=7,
        remnawave_uuid=remnawave_uuid,
        end_date=datetime.now(UTC) - timedelta(hours=1),
        status=SubscriptionStatus.ACTIVE.value,
        user=user,
    )


def _panel_service(*, panel_user=None, error: Exception | None = None):
    """Patch the instance's subscription_service so get_api_client() yields a mock
    api whose get_user_by_uuid is an AsyncMock (returns panel_user or raises error)."""
    api = MagicMock()
    api.get_user_by_uuid = AsyncMock(side_effect=error) if error else AsyncMock(return_value=panel_user)

    @asynccontextmanager
    async def _client():
        yield api

    svc_stub = MagicMock()
    svc_stub.is_configured = True
    svc_stub.get_api_client = _client
    return svc_stub, api


def _service(monkeypatch, *, panel_user=None, error: Exception | None = None):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)
    svc = MonitoringService(bot=MagicMock())
    svc_stub, api = _panel_service(panel_user=panel_user, error=error)
    svc.subscription_service = svc_stub
    svc._send_subscription_expired_notification = AsyncMock()
    svc._log_monitoring_event = AsyncMock()
    return svc, api


def _patch_common(monkeypatch, subs, expire_mock, user):
    monkeypatch.setattr(ms, 'get_expired_subscriptions', AsyncMock(return_value=subs))
    monkeypatch.setattr(ms, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.subscription.is_recently_updated_by_webhook', lambda _s: False)
    monkeypatch.setattr('app.database.crud.subscription.expire_subscription', expire_mock)


async def test_panel_active_future_prevents_false_expiry(monkeypatch):
    panel_expire = datetime.now(UTC) + timedelta(days=30)
    panel_user = SimpleNamespace(status=RemnaWaveUserStatus.ACTIVE, expire_at=panel_expire)
    sub = _sub(remnawave_uuid='LIVE', user_uuid=None)
    svc, api = _service(monkeypatch, panel_user=panel_user)
    expire_mock = AsyncMock()
    _patch_common(monkeypatch, [sub], expire_mock, sub.user)

    await svc._check_expired_subscriptions(_db())

    api.get_user_by_uuid.assert_awaited_once_with('LIVE')
    svc._send_subscription_expired_notification.assert_not_called()
    expire_mock.assert_not_awaited()
    assert sub.status == SubscriptionStatus.ACTIVE.value
    assert sub.end_date == panel_expire  # healed from the panel value
    event_types = [call.args[1] for call in svc._log_monitoring_event.call_args_list]
    assert 'false_expiry_prevented' in event_types


async def test_panel_confirms_expiry_proceeds(monkeypatch):
    sub = _sub(remnawave_uuid='DEAD', user_uuid=None)
    svc, api = _service(monkeypatch, panel_user=None)  # 404 → gone → real expiry
    expire_mock = AsyncMock()
    _patch_common(monkeypatch, [sub], expire_mock, sub.user)

    await svc._check_expired_subscriptions(_db())

    api.get_user_by_uuid.assert_awaited_once_with('DEAD')
    expire_mock.assert_awaited_once()
    svc._send_subscription_expired_notification.assert_awaited_once()


async def test_panel_error_defers_no_notification(monkeypatch):
    sub = _sub(remnawave_uuid='LIVE', user_uuid=None)
    svc, api = _service(monkeypatch, error=RuntimeError('panel down'))
    expire_mock = AsyncMock()
    _patch_common(monkeypatch, [sub], expire_mock, sub.user)

    await svc._check_expired_subscriptions(_db())

    api.get_user_by_uuid.assert_awaited_once_with('LIVE')
    expire_mock.assert_not_awaited()
    svc._send_subscription_expired_notification.assert_not_called()
    assert sub.status == SubscriptionStatus.ACTIVE.value


async def test_no_uuid_proceeds_as_before(monkeypatch):
    sub = _sub(remnawave_uuid=None, user_uuid=None)
    svc, api = _service(monkeypatch, panel_user=MagicMock())
    expire_mock = AsyncMock()
    _patch_common(monkeypatch, [sub], expire_mock, sub.user)

    await svc._check_expired_subscriptions(_db())

    api.get_user_by_uuid.assert_not_called()  # no uuid → no panel lookup
    expire_mock.assert_awaited_once()
    svc._send_subscription_expired_notification.assert_awaited_once()
