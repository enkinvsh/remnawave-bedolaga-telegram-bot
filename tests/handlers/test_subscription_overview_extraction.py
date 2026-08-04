"""Экран «Подписка» (кнопка menu_subscription) — сборка текста.

``show_subscription_info`` принимает ``CallbackQuery``, поэтому превью в
кабинете его не вызовет. Сборка текста вынесена в
``build_subscription_overview_text(user, texts, db)``; хендлер зовёт её же —
копия форматирования разъехалась бы с ботом.

Тесты фиксируют две вещи:

1. **Характеризация** — текст, который хендлер отправляет в Telegram,
   байт-в-байт совпадает с сегодняшним. Проверки зелёные и ДО, и ПОСЛЕ выноса.
2. **Проводка** — билдер существует, отдаёт ровно то же, что уходит в
   ``edit_text``, а новые ключи резолвятся через ``Texts``.
"""

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
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


class _Session:
    def __init__(self, purchases=None):
        self._purchases = purchases or []

    async def refresh(self, *args, **kwargs):
        return None

    async def execute(self, *args, **kwargs):
        purchases = self._purchases

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return purchases

        return _Result()


def _subscription(
    *,
    status: str = 'active',
    is_trial: bool = False,
    days: float = 20,
    traffic_limit_gb: int = 100,
    tariff_id: int | None = None,
):
    subscription = MagicMock()
    subscription.id = 1
    subscription.status = status
    subscription.is_trial = is_trial
    subscription.end_date = datetime.now(UTC) + timedelta(days=days)
    subscription.traffic_limit_gb = traffic_limit_gb
    subscription.traffic_used_gb = 12.5
    subscription.connected_squads = ['squad-a']
    subscription.device_limit = 3
    subscription.tariff_id = tariff_id
    subscription.remnawave_uuid = None
    return subscription


def _user(subscription):
    user = MagicMock()
    user.id = 1
    user.language = 'ru'
    user.full_name = 'Иван Тестовый'
    user.balance_kopeks = 125_000
    user.remnawave_uuid = None
    user.subscription = subscription
    user.get_primary_promo_group = MagicMock(return_value=None)
    return user


def _purchase(traffic_gb: int, days_remaining: int):
    now = datetime.now(UTC)
    return SimpleNamespace(
        traffic_gb=traffic_gb,
        created_at=now - timedelta(days=10),
        expires_at=now + timedelta(days=days_remaining, hours=1),
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Отрезаем панель, синхронизацию и клавиатуру — на текст они не влияют."""
    import app.database.crud.subscription as subscription_crud
    from app.handlers.subscription import purchase

    monkeypatch.setattr(
        subscription_crud,
        'check_and_update_subscription_status',
        AsyncMock(side_effect=lambda _db, subscription: subscription),
    )

    service = MagicMock()
    service.sync_subscription_usage = AsyncMock()
    service.ensure_subscription_synced = AsyncMock(return_value=(True, None))
    monkeypatch.setattr(purchase, 'SubscriptionService', MagicMock(return_value=service))

    monkeypatch.setattr(purchase, 'get_servers_display_names', AsyncMock(return_value='🇳🇱 Нидерланды'))
    monkeypatch.setattr(purchase, 'get_display_subscription_link', MagicMock(return_value=None))
    monkeypatch.setattr(purchase, 'get_subscription_keyboard', MagicMock(return_value=None))
    monkeypatch.setattr(purchase, 'get_back_keyboard', MagicMock(return_value=None))
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)


async def _run_handler(user, db=None) -> str:
    """Прогоняет реальный хендлер и возвращает текст, ушедший в Telegram."""
    from app.handlers.subscription.purchase import show_subscription_info

    message = MagicMock()
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.message = message
    callback.answer = AsyncMock()

    await show_subscription_info(callback, user, db or _Session())

    message.edit_text.assert_awaited_once()
    return message.edit_text.await_args.args[0]


# ============ 1. Характеризация ============


async def test_paid_active_regular_template(texts):
    subscription = _subscription(days=20.5)

    result = await _run_handler(_user(subscription))

    expected = texts.SUBSCRIPTION_OVERVIEW_TEMPLATE.format(
        full_name='Иван Тестовый',
        balance=settings.format_price(125_000),
        status_emoji='💎',
        status_display='Активна',
        warning='',
        tariff_info_block='',
        subscription_type='Платная',
        end_date=format_local_datetime(subscription.end_date, '%d.%m.%Y %H:%M'),
        time_left='20 дн.',
        traffic='12.5 / 100 ГБ',
        servers='🇳🇱 Нидерланды',
        devices_used='',
        device_limit='3',
    )
    assert result == expected


async def test_header_line_comes_from_the_overview_template(texts):
    """Именно эту строку владелец удалял в редакторе и она не исчезала."""
    result = await _run_handler(_user(_subscription()))

    assert 'Информация о подписке' in result
    assert 'Информация о подписке' in texts.SUBSCRIPTION_OVERVIEW_TEMPLATE


@pytest.mark.parametrize('status', ['trial', 'active'])
async def test_trial_status_and_type(status):
    """Триал распознаётся по любому из двух написаний статуса.

    Строку со ``status == 'trial'`` ветка статуса раньше не знала и показывала
    живому триальщику «❓ Неизвестно» — при том что «🎭 Тип» ниже говорил «Триал».
    """
    result = await _run_handler(_user(_subscription(status=status, is_trial=True)))

    assert '📱 Подписка: 🎯 Тестовая' in result
    assert '🎭 Тип: Триал' in result


async def test_unknown_status_stays_unknown():
    """Починка триала не должна съесть саму ветку «Неизвестно»."""
    result = await _run_handler(_user(_subscription(status='pending')))

    assert '📱 Подписка: ❓ Неизвестно' in result


async def test_expired_status():
    result = await _run_handler(_user(_subscription(status='expired', days=-2)))

    assert '📱 Подписка: 🔴 Истекла' in result
    assert '⏰ Осталось: истёк' in result


async def test_limited_status():
    result = await _run_handler(_user(_subscription(status='limited')))

    assert '📱 Подписка: ⚠️ Трафик исчерпан' in result


async def test_disabled_status():
    result = await _run_handler(_user(_subscription(status='disabled')))

    assert '📱 Подписка: ⏸️ Приостановлена' in result


async def test_unlimited_traffic():
    result = await _run_handler(_user(_subscription(traffic_limit_gb=0)))

    assert '📈 Трафик: ∞ (безлимит) | Использовано: 12.5 ГБ' in result


async def test_warning_tomorrow():
    result = await _run_handler(_user(_subscription(days=1.5)))

    assert '\n⚠️ истекает завтра!' in result
    assert '⏰ Осталось: 1 дн.' in result


async def test_warning_today():
    result = await _run_handler(_user(_subscription(days=0.25)))

    assert '\n⚠️ истекает сегодня!' in result
    assert re.search(r'⏰ Осталось: \d+ ч\.', result), result


async def test_connect_link_section(monkeypatch):
    from app.handlers.subscription import purchase

    monkeypatch.setattr(purchase, 'get_display_subscription_link', MagicMock(return_value='https://vpn.example/s/a'))

    result = await _run_handler(_user(_subscription()))

    assert '\n\n🔗 <b>Ссылка для подключения:</b>\n<code>https://vpn.example/s/a</code>' in result
    assert result.endswith('\n\n📱 Скопируйте ссылку и добавьте в ваше VPN приложение')


async def test_no_connect_link_for_expired(monkeypatch):
    from app.handlers.subscription import purchase

    monkeypatch.setattr(purchase, 'get_display_subscription_link', MagicMock(return_value='https://vpn.example/s/a'))

    result = await _run_handler(_user(_subscription(status='expired', days=-2)))

    assert 'Ссылка для подключения' not in result


async def test_devices_hidden_removes_the_line(monkeypatch):
    monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', False)

    result = await _run_handler(_user(_subscription()))

    assert 'Устройства:' not in result


@pytest.mark.parametrize(
    ('days_remaining', 'expected_time_text'),
    [
        (0, 'истекает сегодня'),
        (1, 'остался 1 день'),
        (3, 'осталось 3 дня'),
        (9, 'осталось 9 дней'),
    ],
)
async def test_purchased_traffic_wording(days_remaining, expected_time_text):
    db = _Session(purchases=[_purchase(50, days_remaining)])

    result = await _run_handler(_user(_subscription()), db=db)

    assert '<blockquote>📦 <b>Докупленный трафик:</b>\n' in result
    assert f'• 50 ГБ — {expected_time_text}\n' in result
    assert '% | до ' in result
    assert result.endswith('</blockquote>')


async def test_no_purchased_block_for_unlimited():
    db = _Session(purchases=[_purchase(50, 5)])

    result = await _run_handler(_user(_subscription(traffic_limit_gb=0)), db=db)

    assert 'Докупленный трафик' not in result


# ============ Суточный тариф ============


def _daily_tariff(**overrides):
    tariff = SimpleNamespace(
        id=7,
        name='Дейли',
        is_daily=True,
        traffic_limit_gb=100,
        device_limit=3,
        daily_price_kopeks=1500,
    )
    for key, value in overrides.items():
        setattr(tariff, key, value)
    return tariff


@pytest.fixture
def tariffs_mode(monkeypatch):
    import app.database.crud.tariff as tariff_crud
    from app.utils import promo_offer

    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(promo_offer, 'get_user_active_promo_discount_percent', MagicMock(return_value=0))
    return tariff_crud


async def test_daily_template_is_used(tariffs_mode, monkeypatch, texts):
    subscription = _subscription(tariff_id=7)
    subscription.last_daily_charge_at = None
    subscription.is_daily_paused = False
    monkeypatch.setattr(tariffs_mode, 'get_tariff_by_id', AsyncMock(return_value=_daily_tariff()))

    result = await _run_handler(_user(subscription))

    assert 'Действует до:' not in result
    assert '⏰ Осталось:' not in result
    assert '<b>📦 Дейли</b>' in result
    assert 'Тип: 🔄 Суточный' in result
    assert 'Трафик: 100 ГБ' in result
    assert 'Устройства: 3' in result
    assert 'Цена: 15.00 ₽/день' in result
    assert '⏳ Первое списание скоро' in result


async def test_daily_tariff_unlimited_traffic_line(tariffs_mode, monkeypatch):
    subscription = _subscription(tariff_id=7)
    subscription.last_daily_charge_at = None
    subscription.is_daily_paused = False
    monkeypatch.setattr(tariffs_mode, 'get_tariff_by_id', AsyncMock(return_value=_daily_tariff(traffic_limit_gb=0)))

    result = await _run_handler(_user(subscription))

    assert 'Трафик: ∞ Безлимит' in result


async def test_daily_tariff_paused(tariffs_mode, monkeypatch):
    subscription = _subscription(tariff_id=7)
    subscription.last_daily_charge_at = datetime.now(UTC) - timedelta(hours=2)
    subscription.is_daily_paused = True
    monkeypatch.setattr(tariffs_mode, 'get_tariff_by_id', AsyncMock(return_value=_daily_tariff()))

    result = await _run_handler(_user(subscription))

    assert '⏸️ <b>Подписка приостановлена</b>' in result
    assert '⏳ Осталось: ' in result
    assert '💤 Списание приостановлено' in result


async def test_daily_tariff_next_charge_progress(tariffs_mode, monkeypatch):
    subscription = _subscription(tariff_id=7)
    subscription.last_daily_charge_at = datetime.now(UTC) - timedelta(hours=6)
    subscription.is_daily_paused = False
    monkeypatch.setattr(tariffs_mode, 'get_tariff_by_id', AsyncMock(return_value=_daily_tariff()))

    result = await _run_handler(_user(subscription))

    assert '⏳ До списания: ' in result
    assert '▓' in result or '░' in result
    assert '%' in result


async def test_periodic_tariff_type_line(tariffs_mode, monkeypatch):
    subscription = _subscription(tariff_id=7)
    monkeypatch.setattr(tariffs_mode, 'get_tariff_by_id', AsyncMock(return_value=_daily_tariff(is_daily=False)))

    result = await _run_handler(_user(subscription))

    assert 'Тип: 📅 Периодный' in result
    assert 'Действует до:' in result


# ============ 2. Проводка: билдер общий с хендлером ============


async def test_builder_returns_exactly_what_the_handler_sends(texts):
    from app.handlers.subscription.purchase import build_subscription_overview_text

    subscription = _subscription()
    user = _user(subscription)

    handler_text = await _run_handler(user, db=_Session())
    builder_text = await build_subscription_overview_text(user, texts, _Session())

    assert builder_text == handler_text


async def test_builder_without_subscription_returns_the_none_text(texts):
    from app.handlers.subscription.purchase import build_subscription_overview_text

    user = _user(None)

    assert await build_subscription_overview_text(user, texts, _Session()) == texts.SUBSCRIPTION_NONE


@pytest.mark.parametrize(
    ('key', 'default_marker', 'purchases_days'),
    [
        ('SUBSCRIPTION_PURCHASED_EXPIRES_TODAY', 'истекает сегодня', 0),
        ('SUBSCRIPTION_PURCHASED_ONE_DAY_LEFT', 'остался 1 день', 1),
    ],
)
async def test_purchased_wording_keys_are_overridable(texts, key, default_marker, purchases_days):
    set_override_cache({('ru', key): '!!ЗАМЕНА!!'})
    db = _Session(purchases=[_purchase(50, purchases_days)])

    result = await _run_handler(_user(_subscription()), db=db)

    assert '!!ЗАМЕНА!!' in result
    assert default_marker not in result


async def test_purchased_days_keys_take_the_day_count(texts):
    set_override_cache({('ru', 'SUBSCRIPTION_PURCHASED_MANY_DAYS_LEFT'): 'ещё {days} д.'})
    db = _Session(purchases=[_purchase(50, 9)])

    result = await _run_handler(_user(_subscription()), db=db)

    assert 'ещё 9 д.' in result


async def test_purchased_item_and_progress_keys_are_overridable():
    set_override_cache(
        {
            ('ru', 'SUBSCRIPTION_PURCHASED_TRAFFIC_ITEM'): '- {traffic_gb}GB {time_text}\n',
            ('ru', 'SUBSCRIPTION_PURCHASED_TRAFFIC_PROGRESS'): '  [{bar}] {percent}% -> {expire_date}\n',
        }
    )
    db = _Session(purchases=[_purchase(50, 9)])

    result = await _run_handler(_user(_subscription()), db=db)

    assert '- 50GB осталось 9 дней\n' in result
    assert '% -> ' in result


async def test_daily_tariff_keys_are_overridable(tariffs_mode, monkeypatch):
    set_override_cache(
        {
            ('ru', 'SUBSCRIPTION_TARIFF_TYPE_DAILY'): 'ЕЖЕДНЕВНЫЙ',
            ('ru', 'SUBSCRIPTION_TARIFF_TYPE_LINE'): 'Вид: {tariff_type}',
            ('ru', 'SUBSCRIPTION_TARIFF_DEVICES_LINE'): 'Девайсы: {device_limit}',
            ('ru', 'SUBSCRIPTION_TARIFF_DAILY_PRICE_LINE'): 'Тариф: {price} руб/сутки',
            ('ru', 'SUBSCRIPTION_TARIFF_DAILY_FIRST_CHARGE'): 'Скоро спишем',
        }
    )
    subscription = _subscription(tariff_id=7)
    subscription.last_daily_charge_at = None
    subscription.is_daily_paused = False
    monkeypatch.setattr(tariffs_mode, 'get_tariff_by_id', AsyncMock(return_value=_daily_tariff()))

    result = await _run_handler(_user(subscription))

    assert 'Вид: ЕЖЕДНЕВНЫЙ' in result
    assert 'Девайсы: 3' in result
    assert 'Тариф: 15.00 руб/сутки' in result
    assert 'Скоро спишем' in result


async def test_tariff_name_and_traffic_lines_are_overridable(tariffs_mode, monkeypatch):
    set_override_cache(
        {
            ('ru', 'SUBSCRIPTION_TARIFF_NAME_LINE'): '<b>{tariff_name}</b>',
            ('ru', 'SUBSCRIPTION_TARIFF_TRAFFIC_LINE'): 'Гигабайты: {traffic_gb}',
        }
    )
    subscription = _subscription(tariff_id=7)
    monkeypatch.setattr(tariffs_mode, 'get_tariff_by_id', AsyncMock(return_value=_daily_tariff(is_daily=False)))

    result = await _run_handler(_user(subscription))

    assert '<b>Дейли</b>' in result
    assert 'Гигабайты: 100' in result
