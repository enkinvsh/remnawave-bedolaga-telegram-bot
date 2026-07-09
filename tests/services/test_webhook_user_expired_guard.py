"""Regression: a RemnaWave user.expired webhook must NOT expire (or notify) a
subscription whose LOCAL end_date is still far in the future — such an event is a
stale artefact of panel sync lag. This protection previously covered only DAILY
subscriptions; it now covers ALL tariffs.

Genuinely-expired subs (end_date in the past / within 5 min) keep expiring and
still send WEBHOOK_SUB_EXPIRED.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.remnawave_webhook_service as rw
from app.database.models import SubscriptionStatus


def _service():
    svc = rw.RemnaWaveWebhookService(MagicMock())
    svc._notify_user = AsyncMock()
    svc._get_renew_keyboard = MagicMock(return_value=None)
    svc._stamp_webhook_update = MagicMock()
    return svc


def _sub(*, end_delta: timedelta):
    return SimpleNamespace(
        id=42,
        status=SubscriptionStatus.ACTIVE.value,
        end_date=datetime.now(UTC) + end_delta,
    )


@pytest.mark.asyncio
async def test_expired_webhook_with_future_end_date_skipped():
    user = SimpleNamespace(id=7, telegram_id=123, language='ru')
    sub = _sub(end_delta=timedelta(days=20))
    db = AsyncMock()

    svc = _service()
    with (
        patch.object(rw, 'sa_inspect', return_value=SimpleNamespace(dict={})),
        patch.object(rw, 'expire_subscription', AsyncMock()) as expire,
    ):
        await svc._handle_user_expired(db, user, sub, {'uuid': 'X'})

    expire.assert_not_awaited()
    svc._notify_user.assert_not_awaited()
    svc._stamp_webhook_update.assert_called_once_with(sub)
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_expired_webhook_with_past_end_date_processed():
    user = SimpleNamespace(id=7, telegram_id=123, language='ru')
    sub = _sub(end_delta=timedelta(hours=-1))
    db = AsyncMock()

    svc = _service()
    with (
        patch.object(rw, 'sa_inspect', return_value=SimpleNamespace(dict={})),
        patch.object(rw, 'expire_subscription', AsyncMock()) as expire,
    ):
        await svc._handle_user_expired(db, user, sub, {'uuid': 'X'})

    expire.assert_awaited_once_with(db, sub)
    svc._notify_user.assert_awaited_once()
    assert svc._notify_user.await_args.args[1] == 'WEBHOOK_SUB_EXPIRED'
