"""Sync guard: an ACTIVE subscription's end_date must never be dragged from the
future into the past by a transient panel response.

Regression: ``_update_subscription_from_panel_data`` treats the RemnaWave panel as
authoritative for ACTIVE users and overwrites the local ``end_date`` in BOTH
directions when the diff > 60s. When the panel transiently returns a PAST (or
artificial) ``expireAt`` while still reporting ``status='ACTIVE'``, a valid future
``end_date`` gets pulled into the past — then the hourly monitor sees
``status=active AND end_date<=now`` and fires a false "⛔ Подписка истекла"
notification. This guard blocks exactly that vector: panel expireAt at/before now
AND local end_date strictly after now → keep the local date.

Legitimate forward extension and legitimate backward shortening (both dates in the
future) must still update as before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import app.database.crud.subscription as sub_crud
from app.config import settings
from app.database.models import SubscriptionStatus
from app.services.remnawave_service import RemnaWaveService


def _service() -> RemnaWaveService:
    return RemnaWaveService()


def _make_subscription(service: RemnaWaveService, days_from_now: int) -> MagicMock:
    """Build a subscription whose end_date is a naive datetime in the panel timezone
    (matching how the bot stores dates), offset by ``days_from_now`` days from now.
    """
    panel_now_naive = datetime.now(service._panel_timezone).replace(tzinfo=None)
    subscription = MagicMock()
    subscription.id = 555
    subscription.end_date = panel_now_naive + timedelta(days=days_from_now)
    subscription.status = SubscriptionStatus.ACTIVE.value
    subscription.traffic_used_gb = 0.0
    subscription.connected_squads = []
    subscription.remnawave_uuid = 'uuid-abc'
    subscription.remnawave_short_uuid = 'short-abc'
    subscription.subscription_url = 'https://panel/sub'
    subscription.subscription_crypto_link = ''
    return subscription


def _panel_user(expire_at_utc: datetime) -> dict:
    return {
        'status': 'ACTIVE',
        'expireAt': expire_at_utc.isoformat(),
        'uuid': 'uuid-abc',
        'shortUuid': 'short-abc',
        'subscriptionUrl': 'https://panel/sub',
        'usedTrafficBytes': 0,
        'activeInternalSquads': [],
    }


def _patch_crud(monkeypatch, service: RemnaWaveService, subscription: MagicMock) -> None:
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda _self: False)
    monkeypatch.setattr(
        sub_crud,
        'get_subscription_by_user_id',
        AsyncMock(return_value=subscription),
    )
    monkeypatch.setattr(
        sub_crud,
        'is_recently_updated_by_webhook',
        MagicMock(return_value=False),
    )


async def test_backward_pull_into_past_is_blocked(monkeypatch):
    """Given: local end_date = now + 30d, panel ACTIVE with expireAt = now - 1h.
    When: sync runs. Then: end_date is UNCHANGED (guard blocks the false expire)."""
    service = _service()
    subscription = _make_subscription(service, days_from_now=30)
    original_end = subscription.end_date
    _patch_crud(monkeypatch, service, subscription)

    panel_user = _panel_user(datetime.now(UTC) - timedelta(hours=1))
    await service._update_subscription_from_panel_data(AsyncMock(), MagicMock(id=1, telegram_id=12345), panel_user)

    assert subscription.end_date == original_end


async def test_backward_pull_within_future_allowed(monkeypatch):
    """Given: local end_date = now + 30d, panel expireAt = now + 10d (still future).
    When: sync runs. Then: legitimate shortening updates end_date to ~now + 10d."""
    service = _service()
    subscription = _make_subscription(service, days_from_now=30)
    _patch_crud(monkeypatch, service, subscription)

    expected_utc = datetime.now(UTC) + timedelta(days=10)
    panel_user = _panel_user(expected_utc)
    await service._update_subscription_from_panel_data(AsyncMock(), MagicMock(id=1, telegram_id=12345), panel_user)

    updated_utc = service._local_to_utc(subscription.end_date)
    assert abs((updated_utc - expected_utc).total_seconds()) < 5


async def test_forward_pull_allowed(monkeypatch):
    """Given: local end_date = now + 1d, panel expireAt = now + 30d.
    When: sync runs. Then: forward extension updates end_date to ~now + 30d."""
    service = _service()
    subscription = _make_subscription(service, days_from_now=1)
    _patch_crud(monkeypatch, service, subscription)

    expected_utc = datetime.now(UTC) + timedelta(days=30)
    panel_user = _panel_user(expected_utc)
    await service._update_subscription_from_panel_data(AsyncMock(), MagicMock(id=1, telegram_id=12345), panel_user)

    updated_utc = service._local_to_utc(subscription.end_date)
    assert abs((updated_utc - expected_utc).total_seconds()) < 5
