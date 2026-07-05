"""Агрегатная статистика рекламных кампаний (get_campaigns_aggregate_stats).

ОДИН фиксированный набор сгруппированных запросов, без цикла по кампаниям: старты,
регистрации, трайл-воронка и платящие/выручка — каждый отдельный GROUP BY по всему
набору id, затем сшивается на строки кампаний с фолбэком в нули. Проверяем маппинг и
фолбэки, а не SQL: в окружении нет greenlet, conftest подменяет драйверы БД, поэтому
db.execute мокается по порядку вызовов (как в test_campaign_starts.py).
"""

import types
from unittest.mock import AsyncMock, MagicMock

from app.database.crud.campaign import (
    CampaignAggregateStats,
    get_campaigns_aggregate_stats,
)


def _campaign(**kwargs) -> types.SimpleNamespace:
    base = {
        'id': 1,
        'name': 'Promo',
        'start_parameter': 'promo',
        'bonus_type': 'none',
        'is_active': True,
        'created_at': None,
        'updated_at': None,
    }
    base.update(kwargs)
    return types.SimpleNamespace(**base)


class _Scalars:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)


class _Result:
    def __init__(self, *, rows=(), scalars=None):
        self._rows = list(rows)
        self._scalars = scalars

    def all(self):
        return list(self._rows)

    def scalars(self):
        return _Scalars(self._scalars or [])


def _db_with(*results) -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


async def test_returns_empty_and_skips_aggregates_when_no_campaigns():
    """Нет кампаний → [] и ровно ОДИН запрос (выборка кампаний), без агрегатов."""
    db = _db_with(_Result(scalars=[]))

    result = await get_campaigns_aggregate_stats(db, [999])

    assert result == []
    assert db.execute.await_count == 1


async def test_full_and_empty_campaign_in_one_pass():
    """Кампания с активностью получает точные числа; пустая — нули, а не пропуск строки."""
    active = _campaign(id=1, name='Active', start_parameter='act')
    empty = _campaign(id=2, name='Empty', start_parameter='mt')

    db = _db_with(
        _Result(scalars=[active, empty]),  # 1) кампании
        _Result(rows=[(1, 9, 6)]),  # 2) старты: только id=1 (total=9, unique=6)
        _Result(rows=[(1, 4)]),  # 3) регистрации: id=1 -> 4
        _Result(rows=[(1, 3, 2)]),  # 4) трайл: id=1 -> users=3, activated=2
        _Result(rows=[(1, 2, 150000)]),  # 5) платящие/выручка: id=1 -> 2 юзера, 150000 коп.
    )

    result = await get_campaigns_aggregate_stats(db, [1, 2])

    assert db.execute.await_count == 5
    assert [r.campaign_id for r in result] == [1, 2]

    active_stats, empty_stats = result
    assert active_stats == CampaignAggregateStats(
        campaign_id=1,
        name='Active',
        start_parameter='act',
        bonus_type='none',
        is_active=True,
        created_at=None,
        updated_at=None,
        starts_total=9,
        starts_unique=6,
        registrations=4,
        trial_users=3,
        trial_activated=2,
        paying_users=2,
        total_amount_kopeks=150000,
    )
    assert empty_stats.campaign_id == 2
    assert empty_stats.starts_total == 0
    assert empty_stats.starts_unique == 0
    assert empty_stats.registrations == 0
    assert empty_stats.trial_users == 0
    assert empty_stats.trial_activated == 0
    assert empty_stats.paying_users == 0
    assert empty_stats.total_amount_kopeks == 0


async def test_revenue_kept_in_kopeks_not_rubles():
    """CRUD отдаёт сырые копейки (перевод в рубли — забота слоя представления)."""
    camp = _campaign(id=5, name='Rev', start_parameter='rev')
    db = _db_with(
        _Result(scalars=[camp]),
        _Result(rows=[(5, 1, 1)]),
        _Result(rows=[(5, 1)]),
        _Result(rows=[]),  # трайлов нет → фолбэк в нули
        _Result(rows=[(5, 1, 55912)]),  # 559.12 ₽ = 55912 коп.
    )

    [stats] = await get_campaigns_aggregate_stats(db, [5])

    assert stats.total_amount_kopeks == 55912
    assert stats.trial_users == 0
    assert stats.trial_activated == 0


async def test_all_campaigns_when_ids_none():
    """campaign_ids=None → выбираем все кампании (агрегаты по пустым таблицам = нули)."""
    db = _db_with(
        _Result(scalars=[_campaign(id=1)]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
        _Result(rows=[]),
    )

    result = await get_campaigns_aggregate_stats(db, None)

    assert len(result) == 1
    assert result[0].registrations == 0
    assert result[0].paying_users == 0
