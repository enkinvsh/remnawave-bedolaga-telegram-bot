from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import User
from app.services import winback_oneoff_audience as audience


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _user(user_id: int, *, email: str | None = None, telegram_id: int | None = None) -> User:
    return User(
        id=user_id,
        email=email,
        email_verified=email is not None,
        promo_emails_opt_out_at=None,
        telegram_id=telegram_id,
        status='active',
    )


def _db_rows(rows: list[tuple[User, int | None, bool]]) -> AsyncMock:
    result = MagicMock()
    result.all.return_value = rows
    db = AsyncMock()
    db.execute.return_value = result
    return db


async def _compiled_sql(cohort) -> str:
    db = _db_rows([])
    await audience.select_audience(db, NOW, cohort)
    statement = db.execute.await_args.args[0]
    return str(statement.compile(compile_kwargs={'literal_binds': True})).lower()


async def test_old_depositor_is_selected_by_a_and_excluded_from_trial_cohorts() -> None:
    a_sql = await _compiled_sql(audience.Cohort.A)
    cold_sql = await _compiled_sql(audience.Cohort.C)

    assert "deposits.type = 'deposit'" in a_sql
    assert 'not (exists' not in a_sql.split("deposits.type = 'deposit'", maxsplit=1)[0]
    assert "deposits.type = 'deposit'" in cold_sql
    assert 'not (exists' in cold_sql.split("deposits.type = 'deposit'", maxsplit=1)[0]


async def test_no_trial_cohort_requires_no_trial_subscription() -> None:
    sql = await _compiled_sql(audience.Cohort.B)

    assert 'trial_subscriptions.is_trial is true' in sql
    assert 'not (exists' in sql


@pytest.mark.parametrize(
    ('cohort', 'traffic_predicate'),
    [
        pytest.param('c', '= 0', id='cold-zero-or-null-traffic'),
        pytest.param('d', '> 0', id='warm-positive-traffic'),
    ],
)
async def test_expired_trial_cohorts_split_on_maximum_trial_traffic(cohort: str, traffic_predicate: str) -> None:
    sql = await _compiled_sql(audience.Cohort(cohort))

    assert 'max(trial_subscriptions.end_date)' in sql
    assert "< '2026-07-22 12:00:00+00:00'" in sql
    assert 'max(trial_subscriptions.traffic_used_gb)' in sql
    assert traffic_predicate in sql


@pytest.mark.parametrize('cohort', list('abcd'))
async def test_every_cohort_excludes_active_or_limited_paid_subscriptions(cohort: str) -> None:
    sql = await _compiled_sql(audience.Cohort(cohort))

    assert "active_subscriptions.status in ('active', 'limited')" in sql
    assert 'active_subscriptions.is_trial is false' in sql
    assert "active_subscriptions.end_date > '2026-07-22 12:00:00+00:00'" in sql


async def test_both_channel_user_is_email_preferred_for_trial_cohort() -> None:
    user = _user(1, email='both@example.com', telegram_id=1001)
    db = _db_rows([(user, None, False)])

    selected = await audience.select_audience(db, NOW, audience.Cohort.B)

    assert [(target.user.id, target.channel) for target in selected.targets] == [(1, audience.TargetChannel.EMAIL)]
