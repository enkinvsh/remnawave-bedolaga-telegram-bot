"""Строки статуса подписки на главном экране должны целиком жить в локали.

Часть фрагментов была захардкожена прямо в ``menu.py`` (``' — истекла'``,
``'\\n📦 Тариф: ...'``, фолбэк-имя ``'Подписка'``), поэтому редактор локалей их
не видел и владелец не мог их править.

Тесты фиксируют две вещи:

1. **Характеризация** — вывод каждой ветки ``_get_subscription_status`` и
   ``_get_multi_tariff_status`` байт-в-байт совпадает с тем, что бот рендерит
   сегодня. Эти проверки зелёные и ДО, и ПОСЛЕ выноса строк в локаль.
2. **Проводка** — новые ключи реально резолвятся через ``Texts``: админский
   override меняет вывод. Эти проверки красные, пока строки захардкожены.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.menu import _get_multi_tariff_status, _get_subscription_status, get_main_menu_text
from app.localization.overrides import clear_override_cache, set_override_cache
from app.localization.texts import get_texts
from app.utils.timezone import format_local_datetime


@pytest.fixture(autouse=True)
def _clean_override_cache():
    clear_override_cache()
    yield
    clear_override_cache()


@pytest.fixture
def texts():
    return get_texts('ru')


def _subscription(actual_status: str, *, days_left: int | None = None, is_trial: bool = False):
    subscription = MagicMock()
    subscription.actual_status = actual_status
    subscription.is_trial = is_trial
    if days_left is None:
        subscription.end_date = datetime.now(UTC) - timedelta(days=3)
    else:
        subscription.end_date = datetime.now(UTC) + timedelta(days=days_left, hours=1)
    return subscription


def _user(subscription=None):
    user = MagicMock()
    user.subscription = subscription
    user.id = 777
    user.full_name = 'Иван'
    return user


def _end_text(subscription) -> str:
    return format_local_datetime(subscription.end_date, '%d.%m.%Y')


# ============ 1. Характеризация: _get_subscription_status ============


def test_status_no_subscription(texts):
    assert _get_subscription_status(_user(None), texts) == '❌ Отсутствует'


@pytest.mark.parametrize(
    ('actual_status', 'expected'),
    [
        ('pending', '❌ Нет активной подписки'),
        ('disabled', '⚫ Отключена'),
        ('limited', '⚠️ Трафик исчерпан'),
        ('whatever', '❓ Неизвестно'),
    ],
)
def test_status_simple_branches(texts, actual_status, expected):
    subscription = _subscription(actual_status, days_left=10)
    assert _get_subscription_status(_user(subscription), texts) == expected


def test_status_expired(texts):
    subscription = _subscription('expired')
    expected = f'🔴 Истекла\n📅 {_end_text(subscription)}'
    assert _get_subscription_status(_user(subscription), texts) == expected


@pytest.mark.parametrize('days_left', [5, 2])
def test_status_trial_active(texts, days_left):
    subscription = _subscription('trial', days_left=days_left, is_trial=True)
    expected = f'🎁 Тестовая подписка\n📅 до {_end_text(subscription)} ({days_left} дн.)'
    assert _get_subscription_status(_user(subscription), texts) == expected


def test_status_trial_tomorrow(texts):
    subscription = _subscription('trial', days_left=1, is_trial=True)
    assert _get_subscription_status(_user(subscription), texts) == '🎁 Тестовая подписка\n⚠️ истекает завтра!'


def test_status_trial_today(texts):
    subscription = _subscription('trial', days_left=0, is_trial=True)
    assert _get_subscription_status(_user(subscription), texts) == '🎁 Тестовая подписка\n⚠️ истекает сегодня!'


def test_status_active_daily(texts):
    subscription = _subscription('active', days_left=30)
    assert _get_subscription_status(_user(subscription), texts, is_daily_tariff=True) == '💎 Активна'


def test_status_active_long(texts):
    subscription = _subscription('active', days_left=30)
    expected = f'💎 Активна\n📅 до {_end_text(subscription)} (30 дн.)'
    assert _get_subscription_status(_user(subscription), texts) == expected


def test_status_active_few_days(texts):
    subscription = _subscription('active', days_left=3)
    assert _get_subscription_status(_user(subscription), texts) == '💎 Активна\n⚠️ истекает через 3 дн.'


def test_status_active_tomorrow(texts):
    subscription = _subscription('active', days_left=1)
    assert _get_subscription_status(_user(subscription), texts) == '💎 Активна\n⚠️ истекает завтра!'


def test_status_active_today(texts):
    subscription = _subscription('active', days_left=0)
    assert _get_subscription_status(_user(subscription), texts) == '💎 Активна\n⚠️ истекает сегодня!'


# ============ 2. Характеризация: _get_multi_tariff_status ============


def _multi_sub(actual_status: str, *, days_left: int | None = None, tariff_name: str | None = None):
    subscription = _subscription(actual_status, days_left=days_left)
    subscription.tariff = SimpleNamespace(name=tariff_name) if tariff_name is not None else None
    return subscription


def _patch_subscriptions(monkeypatch, subscriptions):
    import app.database.crud.subscription as subscription_crud

    monkeypatch.setattr(
        subscription_crud,
        'get_all_subscriptions_by_user_id',
        AsyncMock(return_value=subscriptions),
    )


async def test_multi_no_subscriptions(texts, monkeypatch):
    _patch_subscriptions(monkeypatch, [])
    status, block = await _get_multi_tariff_status(_user(), texts, AsyncMock())
    assert status == '❌ Отсутствует'
    assert block == ''


@pytest.mark.parametrize(
    ('actual_status', 'emoji', 'suffix'),
    [
        ('expired', '🔴', ' — истекла'),
        ('disabled', '🔴', ' — отключена'),
        ('limited', '🟡', ' — лимит трафика'),
    ],
)
async def test_multi_status_suffixes(texts, monkeypatch, actual_status, emoji, suffix):
    subscription = _multi_sub(actual_status, tariff_name='Про')
    _patch_subscriptions(monkeypatch, [subscription])

    status, block = await _get_multi_tariff_status(_user(), texts, AsyncMock())

    assert status == f'\n<blockquote>{emoji} <b>Про</b>{suffix}</blockquote>'
    assert block == ''


async def test_multi_active_until(texts, monkeypatch):
    subscription = _multi_sub('active', days_left=12, tariff_name='Про')
    _patch_subscriptions(monkeypatch, [subscription])

    status, _ = await _get_multi_tariff_status(_user(), texts, AsyncMock())

    expected_suffix = f' — до {_end_text(subscription)} (12 дн.)'
    assert status == f'\n<blockquote>🟢 <b>Про</b>{expected_suffix}</blockquote>'


async def test_multi_fallback_tariff_name(texts, monkeypatch):
    subscription = _multi_sub('trial', days_left=4)
    _patch_subscriptions(monkeypatch, [subscription])

    status, _ = await _get_multi_tariff_status(_user(), texts, AsyncMock())

    expected_suffix = f' — до {_end_text(subscription)} (4 дн.)'
    assert status == f'\n<blockquote>🟢 <b>Подписка</b>{expected_suffix}</blockquote>'


async def test_multi_escapes_tariff_name(texts, monkeypatch):
    subscription = _multi_sub('expired', tariff_name='<b>hack</b>')
    _patch_subscriptions(monkeypatch, [subscription])

    status, _ = await _get_multi_tariff_status(_user(), texts, AsyncMock())

    assert status == '\n<blockquote>🔴 <b>&lt;b&gt;hack&lt;/b&gt;</b> — истекла</blockquote>'


# ============ 3. Проводка новых ключей через Texts ============


@pytest.mark.parametrize(
    ('key', 'actual_status'),
    [
        ('SUB_MULTI_SUFFIX_EXPIRED', 'expired'),
        ('SUB_MULTI_SUFFIX_DISABLED', 'disabled'),
        ('SUB_MULTI_SUFFIX_LIMITED', 'limited'),
    ],
)
async def test_multi_suffix_is_overridable(texts, monkeypatch, key, actual_status):
    set_override_cache({('ru', key): ' !!ЗАМЕНА!!'})
    _patch_subscriptions(monkeypatch, [_multi_sub(actual_status, tariff_name='Про')])

    status, _ = await _get_multi_tariff_status(_user(), texts, AsyncMock())

    assert status.endswith(' !!ЗАМЕНА!!</blockquote>'), status


async def test_multi_until_suffix_is_overridable(texts, monkeypatch):
    set_override_cache({('ru', 'SUB_MULTI_SUFFIX_UNTIL'): ' [{end_date}/{days}]'})
    subscription = _multi_sub('active', days_left=6, tariff_name='Про')
    _patch_subscriptions(monkeypatch, [subscription])

    status, _ = await _get_multi_tariff_status(_user(), texts, AsyncMock())

    assert status == f'\n<blockquote>🟢 <b>Про</b> [{_end_text(subscription)}/6]</blockquote>'


async def test_multi_fallback_name_is_overridable(texts, monkeypatch):
    set_override_cache({('ru', 'SUB_MULTI_FALLBACK_NAME'): 'Тариф-заглушка'})
    _patch_subscriptions(monkeypatch, [_multi_sub('expired')])

    status, _ = await _get_multi_tariff_status(_user(), texts, AsyncMock())

    assert '<b>Тариф-заглушка</b>' in status


async def test_main_menu_tariff_line_is_overridable(texts, monkeypatch):
    """Строка '📦 Тариф: X' в одно-тарифном режиме тоже редактируется."""
    import app.database.crud.tariff as tariff_crud
    import app.handlers.menu as menu_module
    from app.config import settings

    set_override_cache({('ru', 'MAIN_MENU_TARIFF_LINE'): '\n[[{tariff_name}]]'})

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)
    monkeypatch.setattr(
        tariff_crud,
        'get_tariff_by_id',
        AsyncMock(return_value=SimpleNamespace(name='Про', is_daily=False)),
    )
    monkeypatch.setattr(menu_module, 'build_promo_offer_hint', AsyncMock(return_value=None))
    monkeypatch.setattr(menu_module, 'build_test_access_hint', AsyncMock(return_value=None))
    monkeypatch.setattr(menu_module, 'get_random_active_message', AsyncMock(return_value=None))

    subscription = _subscription('active', days_left=30)
    subscription.tariff_id = 5
    user = _user(subscription)

    text = await get_main_menu_text(user, texts, AsyncMock())

    assert '[[Про]]' in text
    assert '📦 Тариф:' not in text
