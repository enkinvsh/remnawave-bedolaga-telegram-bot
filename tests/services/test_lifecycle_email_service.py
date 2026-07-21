from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from app.services import lifecycle_email_service as service


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


@dataclass
class FixtureUser:
    id: int
    email: str
    email_verified: bool
    telegram_id: int | None
    status: str
    promo_emails_opt_out_at: datetime | None
    promo_offer_discount_percent: int
    promo_offer_discount_source: str | None
    promo_offer_discount_expires_at: datetime | None
    updated_at: datetime


@dataclass
class FixtureSubscription:
    id: int
    user: FixtureUser
    user_id: int
    is_trial: bool
    status: str
    end_date: datetime
    tariff: Any | None


def _user(**overrides: Any) -> FixtureUser:
    user = FixtureUser(
        id=1,
        email='user@example.com',
        email_verified=True,
        telegram_id=None,
        status='active',
        promo_emails_opt_out_at=None,
        promo_offer_discount_percent=0,
        promo_offer_discount_source=None,
        promo_offer_discount_expires_at=None,
        updated_at=NOW,
    )
    for field_name, value in overrides.items():
        setattr(user, field_name, value)
    return user


def _subscription(user: FixtureUser, **overrides: Any) -> FixtureSubscription:
    subscription = FixtureSubscription(
        id=10,
        user=user,
        user_id=user.id,
        is_trial=True,
        status='expired',
        end_date=NOW - timedelta(days=3),
        tariff=None,
    )
    for field_name, value in overrides.items():
        setattr(subscription, field_name, value)
    return subscription


def _execute_scalars(rows: list[Any]) -> AsyncMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return AsyncMock(return_value=result)


async def test_toggle_off_makes_whole_service_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, 'get_setting_value', AsyncMock(return_value=None))
    db = AsyncMock()

    result = await service.run_lifecycle_emails(db, now=NOW)

    assert result == {}
    db.execute.assert_not_awaited()


@pytest.mark.parametrize(
    ('selector', 'config'),
    [
        (service.select_trial_ending_email, {'hours_before': 2}),
        (
            service.select_post_trial_email,
            {'steps': [{'offset_days': 3, 'discount_percent': 10, 'valid_hours': 24}]},
        ),
    ],
)
async def test_audience_query_requires_verified_email_without_telegram(
    selector,
    config: dict[str, Any],
) -> None:
    db = AsyncMock()
    db.execute = _execute_scalars([])

    await selector(db, config, NOW, 100)

    statement = db.execute.await_args.args[0]
    sql = str(statement)
    assert 'users.email_verified' in sql
    assert 'users.email IS NOT NULL' in sql
    assert 'users.telegram_id IS NULL' in sql


async def test_promo_audience_excludes_opt_out_but_trial_does_not() -> None:
    trial_db = AsyncMock()
    trial_db.execute = _execute_scalars([])
    promo_db = AsyncMock()
    promo_db.execute = _execute_scalars([])

    await service.select_trial_ending_email(trial_db, {'hours_before': 2}, NOW, 100)
    await service.select_post_trial_email(
        promo_db,
        {'steps': [{'offset_days': 3, 'discount_percent': 10, 'valid_hours': 24}]},
        NOW,
        100,
    )

    assert 'promo_emails_opt_out_at' not in str(trial_db.execute.await_args.args[0])
    assert 'users.promo_emails_opt_out_at IS NULL' in str(promo_db.execute.await_args.args[0])


async def test_discount_activation_updates_user_and_marks_offer_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    subscription = _subscription(user)
    offer = SimpleNamespace(
        id=99,
        user_id=user.id,
        notification_type='post_trial_ladder',
        discount_percent=10,
        effect_type='percent_discount',
        expires_at=NOW + timedelta(hours=24),
    )
    upsert = AsyncMock(return_value=offer)
    claimed = AsyncMock(return_value=offer)
    monkeypatch.setattr(service, 'upsert_discount_offer', upsert)
    monkeypatch.setattr(service, 'mark_offer_claimed', claimed)
    db = AsyncMock()

    activated = await service.activate_email_discount(
        db,
        user=user,
        subscription=subscription,
        notification_type='post_trial_ladder',
        discount_percent=10,
        valid_hours=24,
        now=NOW,
    )

    assert activated is offer
    assert user.promo_offer_discount_percent == 10
    assert user.promo_offer_discount_source == 'post_trial_ladder'
    assert user.promo_offer_discount_expires_at == offer.expires_at
    claimed.assert_awaited_once()


async def test_second_run_is_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    subscription = _subscription(user, end_date=NOW + timedelta(hours=1), status='trial')
    candidate = service.EmailLifecycleCandidate(user=user, subscription=subscription, occurrence=1)
    selector = AsyncMock(return_value=[candidate])
    sender = AsyncMock(return_value=True)
    reserve = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(service, 'get_setting_value', AsyncMock(return_value='true'))
    monkeypatch.setattr(service, '_EVENTS', {'trial_ending_email': (selector, sender)})
    monkeypatch.setattr(service, 'get_all_rules', AsyncMock(return_value=[]))
    monkeypatch.setattr(service, 'reserve_send', reserve)
    db = AsyncMock()

    first = await service.run_lifecycle_emails(db, now=NOW)
    second = await service.run_lifecycle_emails(db, now=NOW)

    assert first == {'trial_ending_email': 1}
    assert second == {}
    sender.assert_awaited_once()
    assert reserve.await_args_list[0].args == (db, user.id, 'trial_ending:10_email', 1)


async def test_failed_delivery_releases_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    candidate = service.EmailLifecycleCandidate(user=user, subscription=_subscription(user), occurrence=1)
    sender = AsyncMock(return_value=False)
    release = AsyncMock()
    monkeypatch.setattr(service, 'get_setting_value', AsyncMock(return_value='true'))
    monkeypatch.setattr(service, '_EVENTS', {'trial_ending_email': (AsyncMock(return_value=[candidate]), sender)})
    monkeypatch.setattr(service, 'get_all_rules', AsyncMock(return_value=[]))
    monkeypatch.setattr(service, 'reserve_send', AsyncMock(return_value=True))
    monkeypatch.setattr(service, 'release_send_reservation', release)
    db = AsyncMock()

    result = await service.run_lifecycle_emails(db, now=NOW)

    assert result == {}
    release.assert_awaited_once_with(db, user.id, 'trial_ending:10_email', 1)


async def test_concurrent_runs_send_only_after_single_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    candidate = service.EmailLifecycleCandidate(user=user, subscription=_subscription(user), occurrence=1)
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(service, 'get_setting_value', AsyncMock(return_value='true'))
    monkeypatch.setattr(service, '_EVENTS', {'trial_ending_email': (AsyncMock(return_value=[candidate]), sender)})
    monkeypatch.setattr(service, 'get_all_rules', AsyncMock(return_value=[]))
    monkeypatch.setattr(service, 'reserve_send', AsyncMock(side_effect=[True, False]))
    db = AsyncMock()

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(service.run_lifecycle_emails, db, NOW)
        task_group.start_soon(service.run_lifecycle_emails, db, NOW)

    sender.assert_awaited_once()


async def test_batch_limit_is_applied_after_reserved_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    users = [_user(id=index, email=f'user{index}@example.com') for index in range(1, 4)]
    candidates = [
        service.EmailLifecycleCandidate(user=user, subscription=_subscription(user, id=100 + user.id), occurrence=1)
        for user in users
    ]
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(service, 'get_setting_value', AsyncMock(return_value='true'))
    monkeypatch.setattr(service, '_EVENTS', {'trial_ending_email': (AsyncMock(return_value=candidates), sender)})
    monkeypatch.setattr(service, 'get_all_rules', AsyncMock(return_value=[]))
    monkeypatch.setattr(service, 'reserve_send', AsyncMock(side_effect=[False, True, True]))
    monkeypatch.setattr(service.settings, 'LIFECYCLE_TRIGGERS_BATCH_LIMIT', 2)
    db = AsyncMock()

    result = await service.run_lifecycle_emails(db, now=NOW)

    assert result == {'trial_ending_email': 2}
    assert sender.await_count == 2


async def test_runner_pages_past_fully_reserved_first_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    users = [_user(id=index, email=f'user{index}@example.com') for index in range(1, 5)]
    candidates = [
        service.EmailLifecycleCandidate(user=user, subscription=_subscription(user, id=100 + user.id), occurrence=1)
        for user in users
    ]
    selector = AsyncMock(side_effect=[candidates[:2], candidates[2:]])
    sender = AsyncMock(return_value=True)
    monkeypatch.setattr(service, 'get_setting_value', AsyncMock(return_value='true'))
    monkeypatch.setattr(service, '_EVENTS', {'trial_ending_email': (selector, sender)})
    monkeypatch.setattr(service, 'get_all_rules', AsyncMock(return_value=[]))
    monkeypatch.setattr(service, 'reserve_send', AsyncMock(side_effect=[False, False, True, True]))
    monkeypatch.setattr(service.settings, 'LIFECYCLE_TRIGGERS_BATCH_LIMIT', 2)
    db = AsyncMock()

    result = await service.run_lifecycle_emails(db, now=NOW)

    assert result == {'trial_ending_email': 2}
    assert selector.await_count == 2
    assert selector.await_args_list[0].args[-1] == 0
    assert selector.await_args_list[1].args[-1] == 2


async def test_distinct_subscriptions_use_distinct_dedup_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    candidates = [
        service.EmailLifecycleCandidate(user=user, subscription=_subscription(user, id=10), occurrence=1),
        service.EmailLifecycleCandidate(user=user, subscription=_subscription(user, id=20), occurrence=1),
    ]
    reserve = AsyncMock(return_value=True)
    monkeypatch.setattr(service, 'get_setting_value', AsyncMock(return_value='true'))
    monkeypatch.setattr(service, '_EVENTS', {'trial_ending_email': (AsyncMock(return_value=candidates), AsyncMock(return_value=True))})
    monkeypatch.setattr(service, 'get_all_rules', AsyncMock(return_value=[]))
    monkeypatch.setattr(service, 'reserve_send', reserve)
    db = AsyncMock()

    result = await service.run_lifecycle_emails(db, now=NOW)

    assert result == {'trial_ending_email': 2}
    assert [call.args[2] for call in reserve.await_args_list] == ['trial_ending:10_email', 'trial_ending:20_email']
