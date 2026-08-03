"""Экран «Информация о подписке» должен целиком жить в локали.

``get_subscription_info_text`` несёт ОДИН ключ (``SUBSCRIPTION_INFO``) и два
десятка захардкоженных русских строк — статусы, единицы трафика, автоплатёж,
блок докупленного трафика. Редактор локалей их не видел, и владелец не мог
поправить экран целиком.

Тесты фиксируют две вещи:

1. **Характеризация** — вывод каждой ветки байт-в-байт совпадает с тем, что бот
   рендерит сегодня. Эти проверки зелёные и ДО, и ПОСЛЕ выноса строк.
2. **Проводка** — новые ключи резолвятся через ``Texts``: админский override
   меняет вывод. Эти проверки красные, пока строки захардкожены.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.subscription.pricing import get_subscription_info_text
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


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _Session:
    """Сессия, отвечающая пустым результатом — как реальная БД без покупок."""

    def __init__(self, purchases=None):
        self._purchases = purchases or []

    async def execute(self, *args, **kwargs):
        purchases = self._purchases

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return purchases

        return _Result()


@pytest.fixture(autouse=True)
def _isolate_external_calls(monkeypatch):
    """Отрезаем панель и справочник стран: они не влияют на проверяемые строки."""
    from app.handlers.subscription import pricing

    monkeypatch.setattr(pricing, '_get_countries_info', AsyncMock(return_value=[]))
    monkeypatch.setattr(pricing, 'get_current_devices_count', AsyncMock(return_value='2'))


def _subscription(
    *,
    is_trial: bool = False,
    is_active: bool = True,
    days: int = 20,
    traffic_limit_gb: int = 100,
    autopay: bool = False,
):
    subscription = MagicMock()
    subscription.id = 1
    subscription.is_trial = is_trial
    subscription.is_active = is_active
    subscription.end_date = datetime.now(UTC) + timedelta(days=days, hours=1)
    subscription.days_left = max(0, days)
    subscription.traffic_limit_gb = traffic_limit_gb
    subscription.traffic_used_gb = 12.5
    subscription.connected_squads = ['squad-a', 'squad-b']
    subscription.device_limit = 3
    subscription.autopay_enabled = autopay
    subscription.subscription_url = 'https://vpn.example/sub/abc'
    return subscription


def _user():
    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    user.remnawave_uuid = None
    return user


def _purchase(traffic_gb: int, days_remaining: int):
    now = datetime.now(UTC)
    return SimpleNamespace(
        traffic_gb=traffic_gb,
        created_at=now - timedelta(days=10),
        expires_at=now + timedelta(days=days_remaining, hours=1),
    )


async def _render(subscription, texts, db=None):
    return await get_subscription_info_text(subscription, texts, _user(), db or _Session())


# ============ 1. Характеризация ============


async def test_paid_active_subscription(texts):
    subscription = _subscription()

    result = await _render(subscription, texts)

    expected = texts.SUBSCRIPTION_INFO.format(
        status='✅ Оплачена',
        type='Платная подписка',
        end_date=format_local_datetime(subscription.end_date, '%d.%m.%Y %H:%M'),
        days_left=20,
        traffic_used=texts.format_traffic(12.5, is_limit=False),
        traffic_limit='100 ГБ',
        countries_count=2,
        devices_used='2',
        devices_limit=3,
        autopay_status='⌛ Выключен',
    )
    expected += '\n\n🔗 <b>Ваша ссылка для импорта в VPN приложениe:</b>\n<code>https://vpn.example/sub/abc</code>'

    assert result == expected


async def test_trial_subscription_status_and_type(texts):
    result = await _render(_subscription(is_trial=True), texts)

    assert '<b>Статус:</b> 🎁 Тестовая' in result
    assert '<b>Тип:</b> Триал' in result


async def test_expired_subscription_status(texts):
    result = await _render(_subscription(is_active=False, days=0), texts)

    assert '<b>Статус:</b> ⌛ Истекла' in result
    assert '<b>Тип:</b> Платная подписка' in result


async def test_unlimited_traffic(texts):
    result = await _render(_subscription(traffic_limit_gb=0), texts)

    assert '∞ Безлимитный' in result


async def test_limited_traffic(texts):
    result = await _render(_subscription(traffic_limit_gb=250), texts)

    assert '250 ГБ' in result


async def test_autopay_enabled(texts):
    result = await _render(_subscription(autopay=True), texts)

    assert '<b>Автоплатеж:</b> ✅ Включен' in result


async def test_autopay_disabled(texts):
    result = await _render(_subscription(autopay=False), texts)

    assert '<b>Автоплатеж:</b> ⌛ Выключен' in result


async def test_devices_line_is_rendered(texts):
    result = await _render(_subscription(), texts)

    assert '\n📱 <b>Устройства:</b> 2 / 3' in result


async def test_unlimited_traffic_skips_purchased_block(texts):
    """Блок докупленного трафика только для лимитированных тарифов."""
    db = _Session(purchases=[_purchase(50, 10)])

    result = await _render(_subscription(traffic_limit_gb=0), texts, db=db)

    assert 'Докупленный трафик' not in result


@pytest.mark.parametrize(
    ('days_remaining', 'expected_time_text'),
    [
        (0, 'истекает сегодня'),
        (1, 'остался 1 день'),
        (3, 'осталось 3 дня'),
        (9, 'осталось 9 дней'),
    ],
)
async def test_purchased_traffic_time_wording(texts, days_remaining, expected_time_text):
    db = _Session(purchases=[_purchase(50, days_remaining)])

    result = await _render(_subscription(), texts, db=db)

    assert '\n\n📦 <b>Докупленный трафик:</b>' in result
    assert f'\n• 50 ГБ — {expected_time_text}' in result


async def test_purchased_traffic_progress_bar(texts):
    db = _Session(purchases=[_purchase(50, 10)])

    result = await _render(_subscription(), texts, db=db)

    assert '▰' in result or '▱' in result
    assert '% | до ' in result


async def test_import_link_is_appended(texts):
    result = await _render(_subscription(), texts)

    assert result.endswith(
        '\n\n🔗 <b>Ваша ссылка для импорта в VPN приложениe:</b>\n<code>https://vpn.example/sub/abc</code>'
    )


async def test_missing_url_hides_the_import_link(texts):
    subscription = _subscription()
    subscription.subscription_url = None

    result = await _render(subscription, texts)

    assert 'ссылка для импорта' not in result


# ============ 2. Проводка новых ключей ============


@pytest.mark.parametrize(
    ('key', 'kwargs', 'marker'),
    [
        ('SUBSCRIPTION_INFO_STATUS_TRIAL', {'is_trial': True}, '<b>Статус:</b> !!ЗАМЕНА!!'),
        ('SUBSCRIPTION_INFO_TYPE_TRIAL', {'is_trial': True}, '<b>Тип:</b> !!ЗАМЕНА!!'),
        ('SUBSCRIPTION_INFO_STATUS_PAID', {}, '<b>Статус:</b> !!ЗАМЕНА!!'),
        ('SUBSCRIPTION_INFO_STATUS_EXPIRED', {'is_active': False}, '<b>Статус:</b> !!ЗАМЕНА!!'),
        ('SUBSCRIPTION_INFO_TYPE_PAID', {}, '<b>Тип:</b> !!ЗАМЕНА!!'),
        ('SUBSCRIPTION_INFO_TRAFFIC_UNLIMITED', {'traffic_limit_gb': 0}, '!!ЗАМЕНА!!'),
        ('SUBSCRIPTION_INFO_AUTOPAY_ON', {'autopay': True}, '<b>Автоплатеж:</b> !!ЗАМЕНА!!'),
        ('SUBSCRIPTION_INFO_AUTOPAY_OFF', {}, '<b>Автоплатеж:</b> !!ЗАМЕНА!!'),
    ],
)
async def test_simple_key_is_overridable(texts, key, kwargs, marker):
    set_override_cache({('ru', key): '!!ЗАМЕНА!!'})

    result = await _render(_subscription(**kwargs), texts)

    assert marker in result


async def test_limited_traffic_key_is_overridable(texts):
    set_override_cache({('ru', 'SUBSCRIPTION_INFO_TRAFFIC_LIMITED'): '{traffic_gb} гигов'})

    result = await _render(_subscription(traffic_limit_gb=250), texts)

    assert '250 гигов' in result


async def test_import_link_key_is_overridable(texts):
    set_override_cache({('ru', 'SUBSCRIPTION_INFO_IMPORT_LINK'): '\n\nСсылка: {url}'})

    result = await _render(_subscription(), texts)

    assert result.endswith('\n\nСсылка: https://vpn.example/sub/abc')


async def test_purchased_traffic_keys_are_overridable(texts):
    set_override_cache(
        {
            ('ru', 'SUBSCRIPTION_INFO_PURCHASED_TRAFFIC_TITLE'): '\n\nДОКУПЛЕНО:',
            ('ru', 'SUBSCRIPTION_INFO_PURCHASED_TRAFFIC_ITEM'): '\n- {traffic_gb}GB {time_text}',
            ('ru', 'SUBSCRIPTION_INFO_PURCHASED_MANY_DAYS_LEFT'): 'ещё {days} д.',
            ('ru', 'SUBSCRIPTION_INFO_PURCHASED_TRAFFIC_PROGRESS'): '\n  [{bar}] {percent}% -> {expire_date}',
        }
    )
    db = _Session(purchases=[_purchase(50, 9)])

    result = await _render(_subscription(), texts, db=db)

    assert '\n\nДОКУПЛЕНО:' in result
    assert '\n- 50GB ещё 9 д.' in result
    assert '] ' in result and '% -> ' in result


async def test_monthly_cost_key_is_overridable(texts, monkeypatch):
    from app.handlers.subscription import pricing

    monkeypatch.setattr(pricing, 'get_subscription_cost', AsyncMock(return_value=49900))
    set_override_cache({('ru', 'SUBSCRIPTION_INFO_MONTHLY_COST'): '\nЦена: {price}'})

    result = await _render(_subscription(), texts)

    assert f'\nЦена: {texts.format_price(49900)}' in result


async def test_monthly_cost_line_is_byte_identical(texts, monkeypatch):
    from app.handlers.subscription import pricing

    monkeypatch.setattr(pricing, 'get_subscription_cost', AsyncMock(return_value=49900))

    result = await _render(_subscription(), texts)

    assert f'\n💰 <b>Стоимость подписки в месяц:</b> {texts.format_price(49900)}' in result
