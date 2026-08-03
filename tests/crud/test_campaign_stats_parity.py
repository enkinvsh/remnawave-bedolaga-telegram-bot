"""Числовой паритет старой и новой статистики кампаний НА РЕАЛЬНОЙ БД.

Экран, по которому решают рекламный бюджет. Компилируемый SQL доказывает форму запроса,
но не числа — здесь мы поднимаем настоящий async-движок (SQLite), засеваем строки и
сравниваем ПОКАЗЫВАЕМЫЕ значения старого `get_campaign_statistics` (оракул, не тронут) и
нового батчевого `get_campaigns_page_with_stats`.

Сравнение ТОЧНОЕ, без epsilon: обе стороны округляют одним и тем же Python round(), поэтому
любое расхождение — ошибка проектирования, а допуск спрятал бы ровно тот баг, ради которого
тест написан.

Структурный шаблон — tests/crud/test_locale_override_crud.py.
"""

import sys
from datetime import UTC, datetime, timedelta
from importlib import import_module

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import NullPool

from app.database.crud.campaign import get_campaign_statistics, get_campaigns_page_with_stats
from app.database.crud.transaction import REAL_PAYMENT_METHODS
from app.database.models import (
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    Subscription,
    SubscriptionConversion,
    Transaction,
    TransactionType,
    User,
)


# users.notification_settings — единственная JSONB-колонка среди нужных шести таблиц;
# SQLite её не рендерит. Шим влияет ТОЛЬКО на DDL диалекта sqlite: ни один запрос,
# предикат и агрегат он не трогает, на PostgreSQL остаётся настоящий JSONB.
@compiles(JSONB, 'sqlite')
def _render_jsonb_as_json_on_sqlite(type_, compiler, **kw) -> str:
    return 'JSON'


_TABLES = (
    User.__table__,
    AdvertisingCampaign.__table__,
    AdvertisingCampaignRegistration.__table__,
    Transaction.__table__,
    SubscriptionConversion.__table__,
    Subscription.__table__,
)

_VALID_METHOD = REAL_PAYMENT_METHODS[0]
_COMPARED_FIELDS = ('registrations', 'total_revenue_kopeks', 'paid_users_count', 'conversion_rate')
_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _make_engine(monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.delitem(sys.modules, 'aiosqlite', raising=False)
    import_module('aiosqlite')
    return create_async_engine(
        f'sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true',
        poolclass=NullPool,
    )


def _user(user_id: int, *, has_paid: bool = False) -> User:
    return User(
        id=user_id,
        telegram_id=100000 + user_id,
        username=f'user{user_id}',
        first_name=f'User {user_id}',
        has_had_paid_subscription=has_paid,
    )


def _campaign(campaign_id: int, name: str) -> AdvertisingCampaign:
    return AdvertisingCampaign(
        id=campaign_id,
        name=name,
        start_parameter=f'sp{campaign_id}',
        bonus_type='none',
        is_active=True,
        created_at=_BASE_TIME + timedelta(days=campaign_id),
    )


def _registration(campaign_id: int, user_id: int) -> AdvertisingCampaignRegistration:
    return AdvertisingCampaignRegistration(
        campaign_id=campaign_id,
        user_id=user_id,
        bonus_type='none',
        balance_bonus_kopeks=0,
    )


def _deposit(user_id: int, amount: int, *, completed: bool = True, method: str = _VALID_METHOD) -> Transaction:
    return Transaction(
        user_id=user_id,
        type=TransactionType.DEPOSIT.value,
        amount_kopeks=amount,
        is_completed=completed,
        payment_method=method,
    )


def _subscription_payment(user_id: int, amount: int = 50000, *, completed: bool = True) -> Transaction:
    return Transaction(
        user_id=user_id,
        type=TransactionType.SUBSCRIPTION_PAYMENT.value,
        amount_kopeks=amount,
        is_completed=completed,
        payment_method=_VALID_METHOD,
    )


async def _seed_world(db) -> None:
    """Один общий мир: так проверяется ещё и отсутствие протечки между кампаниями."""
    db.add_all(
        [
            _user(1),  # кейс 1: платёж есть, флага нет
            _user(2, has_paid=True),  # кейс 2: флаг есть, платежа и конверсии нет
            _user(3),  # кейс 3: и платёж, И конверсия — засчитать ОДИН раз
            _user(4),  # кейс 5: зарегистрирован в двух кампаниях
            _user(5),  # кейс 6: фильтрация депозитов
        ]
    )
    db.add_all(
        [
            _campaign(1, 'union-beats-flag'),
            _campaign(2, 'flag-beats-union'),
            _campaign(3, 'union-overlap'),
            _campaign(4, 'no-registrations'),
            _campaign(5, 'shared-user-a'),
            _campaign(6, 'shared-user-b'),
            _campaign(7, 'deposit-filtering'),
        ]
    )
    await db.flush()

    db.add_all(
        [
            _registration(1, 1),
            _registration(2, 2),
            _registration(3, 3),
            _registration(5, 4),
            _registration(6, 4),
            _registration(7, 5),
        ]
    )

    db.add_all(
        [
            _subscription_payment(1),
            # Кейс 3: тот же пользователь в ОБОИХ источниках + два платежа.
            _subscription_payment(3),
            _subscription_payment(3, 70000),
            _deposit(1, 10000),
            _deposit(3, 20000),
            # Кейс 5: один депозит пользователя u4 виден обеим его кампаниям и не задваивается.
            _deposit(4, 30000),
            # Кейс 6: считается ТОЛЬКО валидный депозит.
            _deposit(5, 111111, completed=False),
            _deposit(5, 222222, method='admin_topup'),
            _deposit(5, 40000),
        ]
    )
    db.add(SubscriptionConversion(user_id=3, first_payment_amount_kopeks=70000, converted_at=_BASE_TIME))
    db.add(Subscription(user_id=1, end_date=_BASE_TIME + timedelta(days=30), is_trial=True))
    await db.commit()


async def _assert_full_parity(db) -> list[dict]:
    """Сверяет КАЖДУЮ кампанию и возвращает новые строки для предметных проверок."""
    rows, total = await get_campaigns_page_with_stats(db, offset=0, limit=1000)
    assert total == len(rows), 'total разошёлся с числом строк страницы'

    for row in rows:
        oracle = await get_campaign_statistics(db, row['id'])
        for field in _COMPARED_FIELDS:
            assert oracle[field] == row[field], f'campaign {row["id"]} ({row["name"]}): {field}'
    return rows


async def _seeded_session(monkeypatch: pytest.MonkeyPatch, name: str):
    engine = _make_engine(monkeypatch, name)
    async with engine.connect() as keeper:
        for table in _TABLES:
            await keeper.run_sync(table.create)
        await keeper.commit()
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_every_seeded_campaign_matches_the_oracle_exactly(monkeypatch: pytest.MonkeyPatch):
    """Полный паритет по всем 7 кампаниям сразу — ни одна не исключена из сравнения."""
    async for engine, session_factory in _seeded_session(monkeypatch, 'parity_all'):
        try:
            async with session_factory() as db:
                await _seed_world(db)
                rows = await _assert_full_parity(db)
                assert len(rows) == 7
        finally:
            await engine.dispose()


async def test_case_1_union_beats_flag(monkeypatch: pytest.MonkeyPatch):
    """|A ∪ B| > C: платёж есть, has_had_paid_subscription=False → берём объединение."""
    async for engine, session_factory in _seeded_session(monkeypatch, 'parity_case1'):
        try:
            async with session_factory() as db:
                await _seed_world(db)
                rows = await _assert_full_parity(db)
                [row] = [r for r in rows if r['id'] == 1]

                assert row['registrations'] == 1
                assert row['paid_users_count'] == 1
                assert row['conversion_rate'] == 100.0
                assert row['total_revenue_kopeks'] == 10000
        finally:
            await engine.dispose()


async def test_case_2_flag_beats_union(monkeypatch: pytest.MonkeyPatch):
    """C > |A ∪ B|: только флаг, без платежей и конверсий → максимум берёт флаг."""
    async for engine, session_factory in _seeded_session(monkeypatch, 'parity_case2'):
        try:
            async with session_factory() as db:
                await _seed_world(db)
                rows = await _assert_full_parity(db)
                [row] = [r for r in rows if r['id'] == 2]

                assert row['registrations'] == 1
                assert row['paid_users_count'] == 1
                assert row['total_revenue_kopeks'] == 0
        finally:
            await engine.dispose()


async def test_case_3_user_in_both_sources_counts_once(monkeypatch: pytest.MonkeyPatch):
    """A ∩ B ≠ ∅ — самая важная строка файла.

    У пользователя ДВА SUBSCRIPTION_PAYMENT и запись в subscription_conversions.
    Он обязан быть засчитан ОДИН раз: задвоение раздуло бы conversion_rate до 200-300%
    на экране, по которому решают рекламный бюджет.
    """
    async for engine, session_factory in _seeded_session(monkeypatch, 'parity_case3'):
        try:
            async with session_factory() as db:
                await _seed_world(db)
                rows = await _assert_full_parity(db)
                [row] = [r for r in rows if r['id'] == 3]

                assert row['registrations'] == 1
                assert row['paid_users_count'] == 1, 'пользователь из A и B посчитан дважды'
                assert row['conversion_rate'] == 100.0
        finally:
            await engine.dispose()


async def test_case_4_zero_registrations_is_safe(monkeypatch: pytest.MonkeyPatch):
    """Пустая кампания: без деления на ноль, conversion_rate ровно 0.0."""
    async for engine, session_factory in _seeded_session(monkeypatch, 'parity_case4'):
        try:
            async with session_factory() as db:
                await _seed_world(db)
                rows = await _assert_full_parity(db)
                [row] = [r for r in rows if r['id'] == 4]

                assert row['registrations'] == 0
                assert row['paid_users_count'] == 0
                assert row['conversion_rate'] == 0.0
                assert row['total_revenue_kopeks'] == 0
        finally:
            await engine.dispose()


async def test_case_5_shared_user_does_not_leak_between_campaigns(monkeypatch: pytest.MonkeyPatch):
    """Один пользователь в двух кампаниях: выручка видна обеим и ни одну не раздувает."""
    async for engine, session_factory in _seeded_session(monkeypatch, 'parity_case5'):
        try:
            async with session_factory() as db:
                await _seed_world(db)
                rows = await _assert_full_parity(db)
                first = next(r for r in rows if r['id'] == 5)
                second = next(r for r in rows if r['id'] == 6)

                for row in (first, second):
                    assert row['registrations'] == 1
                    assert row['total_revenue_kopeks'] == 30000
                    assert row['paid_users_count'] == 0
        finally:
            await engine.dispose()


async def test_case_6_only_real_completed_deposits_count(monkeypatch: pytest.MonkeyPatch):
    """Из трёх депозитов засчитан ровно один: не-completed и «не настоящий» метод отброшены."""
    async for engine, session_factory in _seeded_session(monkeypatch, 'parity_case6'):
        try:
            async with session_factory() as db:
                await _seed_world(db)
                rows = await _assert_full_parity(db)
                [row] = [r for r in rows if r['id'] == 7]

                assert row['total_revenue_kopeks'] == 40000
        finally:
            await engine.dispose()


async def test_pagination_is_stable_across_pages_on_equal_sort_keys(monkeypatch: pytest.MonkeyPatch):
    """Ничьи — норма (у большинства кампаний выручка 0), поэтому тайбрейкер id DESC обязателен.

    Проходим весь набор страницами по 2 и требуем: каждая кампания встретилась ровно один
    раз, порядок строго убывает по id. Без тайбрейкера строки дублируются и пропадают —
    компилируемый SQL такое поймать не может.
    """
    async for engine, session_factory in _seeded_session(monkeypatch, 'parity_paging'):
        try:
            async with session_factory() as db:
                await _seed_world(db)

                seen: list[int] = []
                page_size = 2
                for offset in range(0, 8, page_size):
                    rows, total = await get_campaigns_page_with_stats(
                        db, offset=offset, limit=page_size, sort_by='revenue', sort_dir='desc'
                    )
                    assert total == 7
                    seen.extend(row['id'] for row in rows)

                assert len(seen) == len(set(seen)), f'страницы вернули дубликаты: {seen}'
                assert set(seen) == {1, 2, 3, 4, 5, 6, 7}, f'страницы потеряли кампании: {seen}'

                zero_revenue = [cid for cid in seen if cid in {2, 4}]
                assert zero_revenue == sorted(zero_revenue, reverse=True), 'ничьи упорядочены не по id DESC'
        finally:
            await engine.dispose()
