"""CRUD lifecycle-правил и журнала отправок (mocked-session, как остальной crud/).

В окружении нет greenlet + conftest подменяет драйверы БД — проверяем поведение
обёрток (маппинг, самокоммит, fire-and-forget дедуп), а не сам SQL.
"""

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.exc import IntegrityError

from app.database.crud.lifecycle import (
    get_sent_counts,
    record_sent,
    upsert_rule,
    was_sent,
)
from app.database.models import LifecycleMessageLog, LifecycleRule


def _make_db() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _execute_returning(scalar=None, all_rows=None) -> AsyncMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.all.return_value = all_rows or []
    return AsyncMock(return_value=result)


# ── upsert_rule ────────────────────────────────────────────────────────────


async def test_upsert_creates_new_rule_when_absent():
    db = _make_db()
    db.execute = _execute_returning(scalar=None)  # get_rule → None

    rule = await upsert_rule(db, 'expired_second_wave', True, {'discount_percent': 15})

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert isinstance(added, LifecycleRule)
    assert added.key == 'expired_second_wave'
    assert added.enabled is True
    assert added.config == {'discount_percent': 15}
    assert db.commit.await_count == 1
    assert db.refresh.await_count == 1
    assert rule is added


async def test_upsert_updates_existing_rule_in_place():
    db = _make_db()
    existing = MagicMock(spec=LifecycleRule)
    db.execute = _execute_returning(scalar=existing)  # get_rule → existing

    rule = await upsert_rule(db, 'expired_1d', False, {'note': 'x'})

    db.add.assert_not_called()  # уже persistent — только меняем поля
    assert existing.enabled is False
    assert existing.config == {'note': 'x'}
    assert db.commit.await_count == 1
    assert rule is existing


# ── was_sent ───────────────────────────────────────────────────────────────


async def test_was_sent_true_when_row_exists():
    db = _make_db()
    db.execute = _execute_returning(scalar=101)

    assert await was_sent(db, user_id=7, rule_key='trial_ending', occurrence=1) is True


async def test_was_sent_false_when_absent():
    db = _make_db()
    db.execute = _execute_returning(scalar=None)

    assert await was_sent(db, user_id=7, rule_key='trial_ending', occurrence=2) is False


# ── record_sent (fire-and-forget, self-commit, never raise) ────────────────


async def test_record_sent_persists_log_row():
    db = _make_db()

    await record_sent(db, user_id=13, rule_key='post_trial_ladder', occurrence=3)

    db.add.assert_called_once()
    row = db.add.call_args[0][0]
    assert isinstance(row, LifecycleMessageLog)
    assert row.user_id == 13
    assert row.rule_key == 'post_trial_ladder'
    assert row.occurrence == 3
    assert db.commit.await_count == 1
    assert db.rollback.await_count == 0


async def test_record_sent_defaults_occurrence_to_one():
    db = _make_db()

    await record_sent(db, user_id=1, rule_key='expired_1d')

    assert db.add.call_args[0][0].occurrence == 1


async def test_record_sent_swallows_duplicate_and_rolls_back():
    """UNIQUE(user, rule, occurrence) → IntegrityError проглатывается (дедуп)."""
    db = _make_db()
    db.commit = AsyncMock(side_effect=IntegrityError('stmt', {}, Exception('dup')))

    # Не должно бросить.
    await record_sent(db, user_id=1, rule_key='expired_1d', occurrence=1)

    assert db.commit.await_count == 1
    assert db.rollback.await_count == 1


async def test_record_sent_swallows_generic_error_and_rolls_back():
    db = _make_db()
    db.commit = AsyncMock(side_effect=RuntimeError('db down'))

    await record_sent(db, user_id=1, rule_key='expired_1d', occurrence=1)

    assert db.commit.await_count == 1
    assert db.rollback.await_count == 1


# ── get_sent_counts ────────────────────────────────────────────────────────


async def test_get_sent_counts_empty_keys_short_circuits():
    db = _make_db()
    db.execute = AsyncMock()

    result = await get_sent_counts(db, [])

    assert result == {}
    assert db.execute.await_count == 0


async def test_get_sent_counts_aggregates_in_one_query():
    db = _make_db()
    db.execute = _execute_returning(all_rows=[('expired_1d', 3), ('trial_ending', 1)])

    result = await get_sent_counts(db, ['expired_1d', 'trial_ending', 'unused'])

    assert result == {'expired_1d': 3, 'trial_ending': 1}
    assert 'unused' not in result  # без отправок → caller подставит 0
    assert db.execute.await_count == 1
