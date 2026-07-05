"""Lifecycle-триггеры C2 (mocked-session, как остальной services/ и crud/).

Без greenlet/реальной БД: проверяем чистую occurrence-математику, маппинг выборки
кандидатов, дедуп/выключенное правило в раннере, рендер шаблонов, выдачу скидки
лестницей и разграничение аудиторий post_trial_ladder vs платные expired-волны.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services import lifecycle_trigger_messages as ltm, lifecycle_trigger_service as lts
from app.services.lifecycle_rules import merged_config
from app.services.lifecycle_trigger_messages import LifecycleCandidate, render_template
from app.services.lifecycle_trigger_service import (
    compute_ladder_step,
    compute_offset_occurrence,
    compute_repeat_occurrence,
    select_post_trial_ladder,
    select_trial_ending,
    select_trial_not_activated,
    select_trial_zero_traffic,
)


NOW = datetime(2024, 1, 20, 12, 0, 0, tzinfo=UTC)


def _make_user(**overrides) -> SimpleNamespace:
    base = {
        'id': 1,
        'telegram_id': 100,
        'language': 'ru',
        'status': 'active',
        'created_at': NOW - timedelta(hours=2),
        'has_had_paid_subscription': False,
        'lifetime_used_traffic_bytes': 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_sub(user: SimpleNamespace, **overrides) -> SimpleNamespace:
    base = {
        'id': 10,
        'user': user,
        'user_id': user.id,
        'is_trial': True,
        'status': 'active',
        'start_date': NOW - timedelta(hours=2),
        'end_date': NOW + timedelta(hours=1),
        'traffic_used_gb': 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _execute_scalars(rows) -> AsyncMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows
    return AsyncMock(return_value=result)


# ── occurrence-математика (чистые функции) ──────────────────────────────────


def test_repeat_occurrence_first_offset_gives_occ1():
    anchor = NOW - timedelta(hours=1)
    assert compute_repeat_occurrence(anchor, NOW, first_offset_hours=1, repeat_hours=48, max_repeats=3) == 1


def test_repeat_occurrence_second_at_49h_gives_occ2():
    anchor = NOW - timedelta(hours=49)
    assert compute_repeat_occurrence(anchor, NOW, first_offset_hours=1, repeat_hours=48, max_repeats=3) == 2


def test_repeat_occurrence_caps_at_one_plus_max_repeats():
    anchor = NOW - timedelta(hours=1000)
    assert compute_repeat_occurrence(anchor, NOW, first_offset_hours=1, repeat_hours=48, max_repeats=3) == 4


def test_repeat_occurrence_none_before_first_offset():
    anchor = NOW - timedelta(minutes=30)
    assert compute_repeat_occurrence(anchor, NOW, first_offset_hours=1, repeat_hours=48, max_repeats=3) is None


def test_offset_occurrence_maps_to_latest_due():
    anchor = NOW - timedelta(hours=24)
    assert compute_offset_occurrence(anchor, NOW, [1, 24]) == (2, 24)


def test_offset_occurrence_first_window():
    anchor = NOW - timedelta(hours=1)
    assert compute_offset_occurrence(anchor, NOW, [1, 24]) == (1, 1)


def test_offset_occurrence_none_before_first():
    anchor = NOW - timedelta(minutes=30)
    assert compute_offset_occurrence(anchor, NOW, [1, 24]) is None


def test_ladder_step_maps_day_offset():
    steps = [
        {'offset_hours': 1, 'discount_percent': 0},
        {'offset_hours': 24, 'discount_percent': 5},
        {'offset_days': 3, 'discount_percent': 10},
        {'offset_days': 7, 'discount_percent': 15},
        {'offset_days': 14, 'discount_percent': 20},
    ]
    occ, step = compute_ladder_step(NOW - timedelta(days=3), NOW, steps)
    assert occ == 3
    assert step['discount_percent'] == 10


def test_ladder_step_none_before_first():
    steps = [{'offset_hours': 1, 'discount_percent': 0}]
    assert compute_ladder_step(NOW - timedelta(minutes=30), NOW, steps) is None


def test_render_template_substitutes_only_provided():
    out = render_template('Скидка {percent}% на {hours} ч. {days} {date}', percent=5, hours=24)
    assert '5' in out
    assert '24' in out
    assert '{days}' in out
    assert '{date}' in out


# ── выборка кандидатов (mocked db) ──────────────────────────────────────────


async def test_select_trial_not_activated_maps_occurrence():
    user = _make_user(created_at=NOW - timedelta(hours=49))
    db = AsyncMock()
    db.execute = _execute_scalars([user])
    config = merged_config('trial_not_activated', None)

    candidates = await select_trial_not_activated(db, config, NOW, 100)

    assert len(candidates) == 1
    assert candidates[0].user is user
    assert candidates[0].occurrence == 2


async def test_select_trial_not_activated_empty():
    db = AsyncMock()
    db.execute = _execute_scalars([])
    config = merged_config('trial_not_activated', None)

    assert await select_trial_not_activated(db, config, NOW, 100) == []


async def test_select_trial_zero_traffic_maps_offset_occurrence():
    user = _make_user()
    sub = _make_sub(user, start_date=NOW - timedelta(hours=24), end_date=NOW + timedelta(hours=48))
    db = AsyncMock()
    db.execute = _execute_scalars([sub])
    config = merged_config('trial_zero_traffic', None)

    candidates = await select_trial_zero_traffic(db, config, NOW, 100)

    assert len(candidates) == 1
    assert candidates[0].occurrence == 2
    assert candidates[0].offset_hours == 24


async def test_select_trial_ending_occurrence_one():
    user = _make_user()
    sub = _make_sub(user, end_date=NOW + timedelta(hours=1))
    db = AsyncMock()
    db.execute = _execute_scalars([sub])
    config = merged_config('trial_ending', None)

    candidates = await select_trial_ending(db, config, NOW, 100)

    assert len(candidates) == 1
    assert candidates[0].occurrence == 1


async def test_select_post_trial_ladder_maps_step():
    user = _make_user()
    sub = _make_sub(user, status='expired', end_date=NOW - timedelta(days=3))
    db = AsyncMock()
    db.execute = _execute_scalars([sub])
    config = merged_config('post_trial_ladder', None)

    candidates = await select_post_trial_ladder(db, config, NOW, 100)

    assert len(candidates) == 1
    assert candidates[0].occurrence == 3
    assert candidates[0].step['discount_percent'] == 10


async def test_select_post_trial_ladder_scopes_disjoint_from_paid_waves():
    """Аудитория = НЕплатившие (has_had_paid_subscription) с ИСТЁКШИМ ТРИАЛОМ."""
    db = AsyncMock()
    db.execute = _execute_scalars([])
    config = merged_config('post_trial_ladder', None)

    await select_post_trial_ladder(db, config, NOW, 100)

    statement = db.execute.await_args[0][0]
    sql = str(statement)
    params = list(statement.compile().params.values())
    assert 'has_had_paid_subscription' in sql
    assert 'is_trial' in sql
    assert 'expired' in params
    assert 'EXISTS' in sql.upper()


# ── раннер: выключено / дедуп / отправка ────────────────────────────────────


async def test_runner_noop_when_globally_disabled(monkeypatch):
    monkeypatch.setattr(lts.NotificationSettingsService, 'are_notifications_globally_enabled', lambda: False)
    db = AsyncMock()
    send = AsyncMock()

    result = await lts.run_lifecycle_triggers(db, send)

    assert result == {}
    send.assert_not_awaited()
    db.execute.assert_not_called()


async def test_runner_skips_disabled_rule(monkeypatch):
    select_mock = AsyncMock()
    handle_mock = AsyncMock()
    monkeypatch.setattr(lts, '_RULES', {'demo': (select_mock, handle_mock)})
    monkeypatch.setattr(lts.NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    override = SimpleNamespace(key='demo', enabled=False, config={})
    monkeypatch.setattr(lts, 'get_all_rules', AsyncMock(return_value=[override]))
    db = AsyncMock()
    send = AsyncMock()

    result = await lts.run_lifecycle_triggers(db, send)

    select_mock.assert_not_called()
    send.assert_not_awaited()
    assert result == {}


async def test_runner_dedup_skips_when_was_sent(monkeypatch):
    candidate = LifecycleCandidate(user=_make_user(), occurrence=1)
    select_mock = AsyncMock(return_value=[candidate])
    handle_mock = AsyncMock(return_value=True)
    record_mock = AsyncMock()
    monkeypatch.setattr(lts, '_RULES', {'demo': (select_mock, handle_mock)})
    monkeypatch.setattr(lts.NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    monkeypatch.setattr(lts, 'get_all_rules', AsyncMock(return_value=[]))
    monkeypatch.setattr(lts, 'was_sent', AsyncMock(return_value=True))
    monkeypatch.setattr(lts, 'record_sent', record_mock)
    db = AsyncMock()
    send = AsyncMock()

    result = await lts.run_lifecycle_triggers(db, send)

    handle_mock.assert_not_called()
    record_mock.assert_not_awaited()
    assert result == {}


async def test_runner_sends_and_records_when_not_sent(monkeypatch):
    user = _make_user()
    candidate = LifecycleCandidate(user=user, occurrence=1)
    select_mock = AsyncMock(return_value=[candidate])
    handle_mock = AsyncMock(return_value=True)
    record_mock = AsyncMock()
    monkeypatch.setattr(lts, '_RULES', {'demo': (select_mock, handle_mock)})
    monkeypatch.setattr(lts.NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    monkeypatch.setattr(lts, 'get_all_rules', AsyncMock(return_value=[]))
    monkeypatch.setattr(lts, 'was_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(lts, 'record_sent', record_mock)
    db = AsyncMock()
    send = AsyncMock()

    result = await lts.run_lifecycle_triggers(db, send)

    handle_mock.assert_awaited_once()
    record_mock.assert_awaited_once_with(db, user.id, 'demo', 1)
    assert result == {'demo': 1}


async def test_runner_respects_batch_limit(monkeypatch):
    candidates = [LifecycleCandidate(user=_make_user(id=i, telegram_id=100 + i), occurrence=1) for i in range(5)]
    handle_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(lts, '_RULES', {'demo': (AsyncMock(return_value=candidates), handle_mock)})
    monkeypatch.setattr(lts.NotificationSettingsService, 'are_notifications_globally_enabled', lambda: True)
    monkeypatch.setattr(lts, 'get_all_rules', AsyncMock(return_value=[]))
    monkeypatch.setattr(lts, 'was_sent', AsyncMock(return_value=False))
    monkeypatch.setattr(lts, 'record_sent', AsyncMock())
    monkeypatch.setattr(lts.settings, 'LIFECYCLE_TRIGGERS_BATCH_LIMIT', 2)
    db = AsyncMock()
    send = AsyncMock()

    result = await lts.run_lifecycle_triggers(db, send)

    assert result == {'demo': 2}
    assert handle_mock.await_count == 2


# ── обработчики: рендер / скидка / коордиинация с легаси ─────────────────────


async def test_handle_trial_not_activated_first_uses_base_template():
    config = merged_config('trial_not_activated', None)
    send = AsyncMock()
    candidate = LifecycleCandidate(user=_make_user(), occurrence=1)

    ok = await ltm.handle_trial_not_activated(AsyncMock(), send, candidate, config, NOW)

    assert ok is True
    assert send.await_args.kwargs['text'] == config['message_template']


async def test_handle_trial_not_activated_repeat_uses_repeat_template():
    config = merged_config('trial_not_activated', None)
    send = AsyncMock()
    candidate = LifecycleCandidate(user=_make_user(), occurrence=2)

    await ltm.handle_trial_not_activated(AsyncMock(), send, candidate, config, NOW)

    assert send.await_args.kwargs['text'] == config['repeat_message_template']


async def test_handle_post_trial_ladder_grants_discount(monkeypatch):
    user = _make_user()
    sub = _make_sub(user, status='expired', end_date=NOW - timedelta(hours=24))
    step = {'offset_hours': 24, 'discount_percent': 5, 'valid_hours': 24}
    candidate = LifecycleCandidate(user=user, occurrence=2, subscription=sub, step=step)
    offer = SimpleNamespace(id=555, expires_at=NOW + timedelta(hours=24))
    upsert_mock = AsyncMock(return_value=offer)
    monkeypatch.setattr(ltm, 'upsert_discount_offer', upsert_mock)
    config = merged_config('post_trial_ladder', None)
    send = AsyncMock()

    ok = await ltm.handle_post_trial_ladder(AsyncMock(), send, candidate, config, NOW)

    assert ok is True
    upsert_mock.assert_awaited_once()
    assert upsert_mock.await_args.kwargs['discount_percent'] == 5
    assert upsert_mock.await_args.kwargs['notification_type'] == 'post_trial_ladder'
    keyboard = send.await_args.kwargs['reply_markup']
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert '🎁 Получить скидку' in labels


async def test_handle_post_trial_ladder_zero_step_no_discount(monkeypatch):
    user = _make_user()
    sub = _make_sub(user, status='expired', end_date=NOW - timedelta(hours=1))
    step = {'offset_hours': 1, 'discount_percent': 0}
    candidate = LifecycleCandidate(user=user, occurrence=1, subscription=sub, step=step)
    upsert_mock = AsyncMock()
    monkeypatch.setattr(ltm, 'upsert_discount_offer', upsert_mock)
    config = merged_config('post_trial_ladder', None)
    send = AsyncMock()

    ok = await ltm.handle_post_trial_ladder(AsyncMock(), send, candidate, config, NOW)

    assert ok is True
    upsert_mock.assert_not_awaited()
    assert send.await_args.kwargs['text'] == config['message_template_no_discount']


async def test_handle_trial_ending_skips_when_legacy_already_sent(monkeypatch):
    monkeypatch.setattr(ltm, 'notification_sent', AsyncMock(return_value=True))
    config = merged_config('trial_ending', None)
    send = AsyncMock()
    candidate = LifecycleCandidate(user=_make_user(), occurrence=1, subscription=_make_sub(_make_user()))

    ok = await ltm.handle_trial_ending(AsyncMock(), send, candidate, config, NOW)

    assert ok is True
    send.assert_not_awaited()


async def test_handle_trial_ending_sends_and_records_legacy_marker(monkeypatch):
    monkeypatch.setattr(ltm, 'notification_sent', AsyncMock(return_value=False))
    record_legacy = AsyncMock()
    monkeypatch.setattr(ltm, 'record_notification', record_legacy)
    config = merged_config('trial_ending', None)
    send = AsyncMock()
    candidate = LifecycleCandidate(user=_make_user(), occurrence=1, subscription=_make_sub(_make_user()))

    ok = await ltm.handle_trial_ending(AsyncMock(), send, candidate, config, NOW)

    assert ok is True
    send.assert_awaited_once()
    assert '2' in send.await_args.kwargs['text']
    record_legacy.assert_awaited_once()
