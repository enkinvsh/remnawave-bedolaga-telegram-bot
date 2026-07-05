"""Трекинг стартов рекламных кампаний (advertising_campaign_starts).

Сырые события: КАЖДЫЙ заход по рекламной ссылке (новый И существующий юзер, бот и
кабинет). Раньше существующий юзер по рекламной ссылке не оставлял следа — это и
есть дыра «0 рег.» в дашбордах. Запись fire-and-forget: сбой вставки не должен
ломать /start или вход в кабинет. Уникальность считается на этапе запроса.

Мок-сессии (не реальная БД): в этом окружении нет greenlet, а conftest подменяет
драйверы БД — как и остальные тесты crud/, проверяем поведение обёртки, а не SQL.
"""

from unittest.mock import AsyncMock, MagicMock

from app.database.crud.campaign_starts import (
    get_campaign_start_counts,
    record_campaign_start,
)
from app.database.models import AdvertisingCampaignStart


def _make_db() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


def _added_row(db: AsyncMock) -> AdvertisingCampaignStart:
    db.add.assert_called_once()
    return db.add.call_args[0][0]


async def test_record_start_for_existing_user_persists_row():
    """Существующий юзер по рекламной ссылке ТОЖЕ пишется (закрытие дыры «0 рег.»)."""
    db = _make_db()

    await record_campaign_start(db, 42, telegram_id=7_790_427_779, user_id=13, source='cabinet')

    row = _added_row(db)
    assert isinstance(row, AdvertisingCampaignStart)
    assert row.campaign_id == 42
    assert row.telegram_id == 7_790_427_779
    assert row.user_id == 13
    assert row.source == 'cabinet'
    assert db.commit.await_count == 1
    assert db.rollback.await_count == 0


async def test_record_start_for_anonymous_telegram_id():
    """Новый /start: юзера в БД ещё нет — пишем по telegram_id, user_id=None."""
    db = _make_db()

    await record_campaign_start(db, 7, telegram_id=555, user_id=None, source='bot')

    row = _added_row(db)
    assert row.campaign_id == 7
    assert row.telegram_id == 555
    assert row.user_id is None
    assert row.source == 'bot'
    assert db.commit.await_count == 1


async def test_record_start_swallows_failure_and_rolls_back():
    """DB-хиккап на commit НЕ пробрасывается наружу (иначе /start бы падал) + rollback."""
    db = _make_db()
    db.commit = AsyncMock(side_effect=RuntimeError('db down'))

    # Не должно бросить исключение.
    await record_campaign_start(db, 1, telegram_id=1, user_id=1, source='bot')

    assert db.commit.await_count == 1
    assert db.rollback.await_count == 1


async def test_get_start_counts_empty_ids_short_circuits():
    """Пустой список кампаний → {} без единого запроса к БД."""
    db = _make_db()
    db.execute = AsyncMock()

    result = await get_campaign_start_counts(db, [])

    assert result == {}
    assert db.execute.await_count == 0


async def test_get_start_counts_aggregates_total_and_unique_in_one_query():
    """total ≠ unique сохраняется в маппинге; ОДИН сгруппированный запрос, не N+1."""
    db = _make_db()
    grouped = MagicMock()
    grouped.all.return_value = [(1, 5, 3), (2, 2, 2)]
    db.execute = AsyncMock(return_value=grouped)

    result = await get_campaign_start_counts(db, [1, 2, 999])

    assert result == {1: (5, 3), 2: (2, 2)}
    # Кампания 999 без стартов в словарь не попала — caller подставит (0, 0).
    assert 999 not in result
    assert db.execute.await_count == 1
