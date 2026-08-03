"""Батчевая страница списка кампаний (get_campaigns_page_with_stats).

Заменяет цикл `get_campaign_statistics` по каждой кампании. Экран, по которому решают
рекламный бюджет, поэтому проверяем не «работает», а ПОБАЙТОВОЕ совпадение отображаемых
чисел со старой реализацией: то же округление (питоновское, не SQL), тот же фолбэк имени
партнёра (питоновская ложность пустой строки, не COALESCE) — и устойчивую пагинацию.

Реальной БД в окружении нет (conftest подменяет драйверы), поэтому db.execute мокается по
порядку вызовов, как в test_campaign_aggregate_stats.py, а SQL проверяется компиляцией в
диалект PostgreSQL.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

from app.database.crud.campaign import _campaign_filters, get_campaigns_page_with_stats


_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _row(
    *,
    campaign_id: int = 1,
    registrations: int = 0,
    revenue: int = 0,
    paid_users: int = 0,
    partner_id: int | None = None,
    partner_first_name: str | None = None,
    partner_username: str | None = None,
) -> tuple:
    return (
        campaign_id,
        'Promo',
        'promo',
        'none',
        True,
        partner_id,
        _CREATED_AT,
        registrations,
        revenue,
        paid_users,
        partner_id,
        partner_first_name,
        partner_username,
    )


class _PageResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _CountResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


def _db(rows, total: int) -> MagicMock:
    """Мок сессии, который ЗАПОМИНАЕТ выполненные statement'ы для проверки SQL."""
    db = MagicMock()
    db.executed = []

    async def _execute(stmt):
        db.executed.append(stmt)
        return _PageResult(rows) if len(db.executed) == 1 else _CountResult(total)

    db.execute = AsyncMock(side_effect=_execute)
    return db


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


async def test_conversion_rate_uses_python_rounding_not_sql():
    """paid=1 / regs=16 → 6.2 (банковское округление Python), а НЕ 6.3 (round() в PostgreSQL).

    Это ровно тот случай, на котором расходятся два округления. Если тест упал на 6.3 —
    отображаемую конверсию посчитали в SQL, и числа на экране поехали.
    """
    db = _db([_row(registrations=16, paid_users=1)], total=1)

    [row], _ = await get_campaigns_page_with_stats(db)

    assert row['conversion_rate'] == 6.2


async def test_zero_registrations_gives_zero_rate_without_dividing():
    """Пустая кампания → 0.0, а не ZeroDivisionError."""
    db = _db([_row(registrations=0, paid_users=0)], total=1)

    [row], _ = await get_campaigns_page_with_stats(db)

    assert row['conversion_rate'] == 0.0
    assert row['registrations'] == 0
    assert row['paid_users_count'] == 0


async def test_empty_first_name_falls_through_to_username():
    """Пустое first_name ложно в Python → берём username. SQL COALESCE вернул бы ''."""
    db = _db([_row(partner_id=7, partner_first_name='', partner_username='refer')], total=1)

    [row], _ = await get_campaigns_page_with_stats(db)

    assert row['partner_name'] == 'refer'


async def test_partner_without_name_or_username_falls_back_to_id():
    db = _db([_row(partner_id=7, partner_first_name=None, partner_username=None)], total=1)

    [row], _ = await get_campaigns_page_with_stats(db)

    assert row['partner_name'] == '#7'


async def test_campaign_without_partner_has_no_partner_name():
    db = _db([_row(partner_id=None)], total=1)

    [row], _ = await get_campaigns_page_with_stats(db)

    assert row['partner_name'] is None
    assert row['partner_user_id'] is None


async def test_two_round_trips_regardless_of_page_size():
    """Ровно 2 запроса на страницу — ради этого и переписывали (было ~905)."""
    rows = [_row(campaign_id=i) for i in range(100)]
    db = _db(rows, total=100)

    items, total = await get_campaigns_page_with_stats(db, limit=100)

    assert db.execute.await_count == 2
    assert len(items) == 100
    assert total == 100


async def test_page_and_count_share_the_same_filters():
    """Один и тот же набор предикатов в странице и в total — иначе пагинация разъедется."""
    db = _db([], total=0)

    await get_campaigns_page_with_stats(db, include_inactive=False, search='promo', partner_user_id=7)

    page_sql, count_sql = (_sql(stmt) for stmt in db.executed)
    for predicate in ('is_active IS true', 'partner_user_id =', 'ILIKE'):
        assert predicate in page_sql
        assert predicate in count_sql


async def test_paid_users_is_a_portable_two_way_max():
    """max(|A ∪ B|, C) выражен через CASE, а не через PostgreSQL-only greatest().

    Числовой паритет проверяется на реальной БД в test_campaign_stats_parity.py — и он
    исполняет ЭТО выражение на SQLite, где greatest() не существует. Здесь фиксируем, что
    двусторонний максимум никуда не делся и остаётся переносимым.
    """
    db = _db([], total=0)

    await get_campaigns_page_with_stats(db)

    page_sql = _sql(db.executed[0])
    union_side = 'count(DISTINCT paid_union.user_id)'
    flag_side = 'count(DISTINCT CASE WHEN (users.has_had_paid_subscription IS true)'
    assert f'CASE WHEN ({union_side} > {flag_side}' in page_sql
    assert f'THEN {union_side} ELSE {flag_side}' in page_sql
    assert 'greatest(' not in page_sql


async def test_paid_union_deduplicates_rows():
    """UNION, а не UNION ALL.

    Это гарантия ПЛАНА, не числа: count(DISTINCT ...) инвариантен к дублированию строк,
    поэтому одна лишь замена на UNION ALL цифры не сдвинет (проверено мутацией). Но она
    снимает один из двух слоёв дедупликации, и вместе с потерей DISTINCT даёт задвоение
    платящих. Держим оба слоя.
    """
    db = _db([], total=0)

    await get_campaigns_page_with_stats(db)

    page_sql = _sql(db.executed[0])
    assert ' UNION SELECT subscription_conversions.user_id' in page_sql
    assert 'UNION ALL' not in page_sql


def _status_predicates(db: MagicMock) -> list[str]:
    """Статус-предикат из КАЖДОГО выполненного запроса (страница и total) — они обязаны совпадать."""
    return [
        'IS true'
        if 'advertising_campaigns.is_active IS true' in sql
        else 'IS false'
        if 'advertising_campaigns.is_active IS false' in sql
        else 'none'
        for sql in (_sql(stmt) for stmt in db.executed)
    ]


async def test_status_any_adds_no_predicate():
    """is_active=None → фильтра по статусу нет, отдаём и активные, и выключенные."""
    db = _db([], total=0)

    await get_campaigns_page_with_stats(db, is_active=None)

    assert _status_predicates(db) == ['none', 'none']


async def test_status_active_only():
    db = _db([], total=0)

    await get_campaigns_page_with_stats(db, is_active=True)

    assert _status_predicates(db) == ['IS true', 'IS true']


async def test_status_inactive_only():
    """Ради этого состояния и вводили трёхпозиционный фильтр: старый флаг его не выражал."""
    db = _db([], total=0)

    await get_campaigns_page_with_stats(db, is_active=False)

    assert _status_predicates(db) == ['IS false', 'IS false']


async def test_explicit_is_active_wins_over_include_inactive():
    """include_inactive=False сам по себе означает «только активные» — но is_active главнее.

    Иначе UI не смог бы запросить выключенные кампании, не зная про устаревший флаг.
    """
    db = _db([], total=0)

    await get_campaigns_page_with_stats(db, is_active=False, include_inactive=False)

    assert _status_predicates(db) == ['IS false', 'IS false']


async def test_old_include_inactive_contract_is_untouched():
    """Клиент, который шлёт только старый флаг, получает ровно прежнее поведение."""
    active_only = _db([], total=0)
    everything = _db([], total=0)

    await get_campaigns_page_with_stats(active_only, include_inactive=False)
    await get_campaigns_page_with_stats(everything, include_inactive=True)

    assert _status_predicates(active_only) == ['IS true', 'IS true']
    assert _status_predicates(everything) == ['none', 'none']


async def test_ordering_always_carries_the_id_tiebreaker():
    """У большинства кампаний выручка и регистрации нулевые — ничьи это норма, а не край.

    Без стабильного тайбрейкера строки дублируются и пропадают между страницами.
    """
    for sort_by in ('created_at', 'name', 'registrations', 'revenue', 'conversion'):
        for sort_dir in ('asc', 'desc'):
            db = _db([], total=0)

            await get_campaigns_page_with_stats(db, sort_by=sort_by, sort_dir=sort_dir)

            page_sql = _sql(db.executed[0])
            assert 'advertising_campaigns.id DESC \n LIMIT' in page_sql, (sort_by, sort_dir)


async def test_unknown_sort_by_falls_back_to_created_at():
    db = _db([], total=0)
    reference = _db([], total=0)

    await get_campaigns_page_with_stats(db, sort_by='; DROP TABLE users')
    await get_campaigns_page_with_stats(reference, sort_by='created_at')

    assert _sql(db.executed[0]) == _sql(reference.executed[0])


def test_filters_are_empty_when_nothing_is_requested():
    """Клиент старого API не шлёт новые параметры → фильтров нет, поведение прежнее."""
    assert _campaign_filters() == []


def test_blank_search_is_not_a_filter():
    """Пробелы в поиске не должны превращаться в ILIKE '%%' и резать выдачу."""
    assert _campaign_filters(search='   ') == []
