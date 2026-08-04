"""Скрытие строки «Устройства» на экранах подписки — во ВСЕХ локалях.

Строку вырезали ``.replace()`` по русскому литералу
``'\\n📱 Устройства: {devices_used} / {device_limit}'``. Замена шла по СЫРОМУ
шаблону (до ``.format()``), а в локалях ``en/ua/fa/zh`` подпись переведена —
иголка не совпадала, и строка протекала в четырёх языках из пяти.

Тесты фиксируют переход на вырезание по ПЛЕЙСХОЛДЕРУ ``{devices_used}``: он
одинаков во всех локалях и переживает правки текста в редакторе локалей.

1. Шаблонный уровень — все 5 локалей × все 3 ключа на РЕАЛЬНЫХ строках из
   ``app/localization/locales/*.json``.
2. Байт-в-байт регрессия для ``ru``: новый хелпер обязан дать ровно то же,
   что давала старая ``.replace()``.
3. Сквозные проверки обоих мест вызова на украинской локали.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.handlers.subscription.purchase import _drop_rows_with_placeholder
from app.localization.overrides import clear_override_cache
from app.localization.texts import get_texts


LOCALES = ('ru', 'en', 'ua', 'fa', 'zh')

# ключ -> (плейсхолдер лимита, соседний плейсхолдер, который обязан уцелеть)
TEMPLATE_KEYS = {
    'SUBSCRIPTION_OVERVIEW_TEMPLATE': ('{device_limit}', '{traffic}'),
    'SUBSCRIPTION_DAILY_OVERVIEW_TEMPLATE': ('{device_limit}', '{traffic}'),
    'SUBSCRIPTION_SETTINGS_OVERVIEW': ('{devices_limit}', '{traffic_used}'),
}

# Иголки старой ``.replace()`` — регрессионный эталон для русского.
LEGACY_NEEDLES = {
    'SUBSCRIPTION_OVERVIEW_TEMPLATE': '\n📱 Устройства: {devices_used} / {device_limit}',
    'SUBSCRIPTION_DAILY_OVERVIEW_TEMPLATE': '\n📱 Устройства: {devices_used} / {device_limit}',
    'SUBSCRIPTION_SETTINGS_OVERVIEW': '\n📱 Устройства: {devices_used} / {devices_limit}',
}


@pytest.fixture(autouse=True)
def _clean_override_cache():
    clear_override_cache()
    yield
    clear_override_cache()


class _Session:
    """Заглушка сессии: строка «Устройства» от БД не зависит."""

    async def refresh(self, *args, **kwargs):
        return None

    async def execute(self, *args, **kwargs):
        class _Result:
            def scalars(self):
                return self

            def all(self):
                return []

        return _Result()


def _subscription():
    subscription = MagicMock()
    subscription.id = 1
    subscription.status = 'active'
    subscription.is_trial = False
    subscription.end_date = datetime.now(UTC) + timedelta(days=20)
    subscription.traffic_limit_gb = 100
    subscription.traffic_used_gb = 12.5
    subscription.connected_squads = ['squad-a']
    subscription.device_limit = 3
    subscription.tariff_id = None
    subscription.remnawave_uuid = None
    return subscription


def _user(subscription):
    user = MagicMock()
    user.id = 1
    user.language = 'ua'
    user.full_name = 'Іван Тестовий'
    user.balance_kopeks = 125_000
    user.remnawave_uuid = None
    user.subscription = subscription
    user.get_primary_promo_group = MagicMock(return_value=None)
    return user


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Отрезаем панель, БД и клавиатуры — на строку «Устройства» они не влияют."""
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

    monkeypatch.setattr(purchase, 'get_servers_display_names', AsyncMock(return_value='🇳🇱 Нідерланди'))
    monkeypatch.setattr(purchase, 'get_display_subscription_link', MagicMock(return_value=None))
    monkeypatch.setattr(purchase, 'get_subscription_keyboard', MagicMock(return_value=None))
    monkeypatch.setattr(purchase, 'get_back_keyboard', MagicMock(return_value=None))
    monkeypatch.setattr(purchase, '_should_show_countries_management', AsyncMock(return_value=False))
    monkeypatch.setattr(purchase, 'get_updated_subscription_settings_keyboard', MagicMock(return_value=None))
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)


# ============ 1. Шаблонный уровень: все локали × все ключи ============


@pytest.mark.parametrize('locale', LOCALES)
@pytest.mark.parametrize('key', sorted(TEMPLATE_KEYS))
def test_devices_row_is_dropped_in_every_locale(locale, key):
    """Плейсхолдеры устройств уходят, соседняя строка остаётся — в любой локали."""
    limit_placeholder, survivor = TEMPLATE_KEYS[key]
    template = get_texts(locale).t(key, '')
    assert '{devices_used}' in template, f'локаль {locale} потеряла строку устройств в {key}'

    result = _drop_rows_with_placeholder(template, '{devices_used}')

    assert '{devices_used}' not in result
    assert limit_placeholder not in result
    assert survivor in result


# ============ 2. Байт-в-байт регрессия для русского ============


@pytest.mark.parametrize('key', sorted(TEMPLATE_KEYS))
def test_russian_output_matches_the_legacy_replace(key):
    """Русский рендер не должен сдвинуться ни на символ."""
    template = get_texts('ru').t(key, '')
    legacy = template.replace(LEGACY_NEEDLES[key], '')

    assert _drop_rows_with_placeholder(template, '{devices_used}') == legacy


# ============ 3. Сквозная проверка: экран «Подписка» ============


async def test_overview_hides_devices_row_in_ukrainian(monkeypatch):
    monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', False)
    from app.handlers.subscription.purchase import build_subscription_overview_text

    result = await build_subscription_overview_text(_user(_subscription()), get_texts('ua'), _Session())

    assert 'Пристрої' not in result
    assert 'Сервери:' in result


async def test_overview_keeps_devices_row_when_selection_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', True)
    from app.handlers.subscription.purchase import build_subscription_overview_text

    result = await build_subscription_overview_text(_user(_subscription()), get_texts('ua'), _Session())

    assert 'Пристрої' in result


# ============ 4. Сквозная проверка: экран «Настройки подписки» ============


async def _run_settings_handler(db_user) -> str:
    from app.handlers.subscription.purchase import handle_subscription_settings

    message = MagicMock()
    message.edit_text = AsyncMock()
    callback = MagicMock()
    callback.message = message
    callback.answer = AsyncMock()

    await handle_subscription_settings(callback, db_user, _Session())

    message.edit_text.assert_awaited_once()
    return message.edit_text.await_args.args[0]


async def test_settings_hides_devices_row_in_ukrainian(monkeypatch):
    monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', False)

    result = await _run_settings_handler(_user(_subscription()))

    assert 'Пристрої' not in result
    assert 'Трафік:' in result


async def test_settings_keeps_devices_row_when_selection_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'DEVICES_SELECTION_ENABLED', True)
    from app.handlers.subscription import purchase

    monkeypatch.setattr(purchase, 'get_current_devices_count', AsyncMock(return_value=2))

    result = await _run_settings_handler(_user(_subscription()))

    assert 'Пристрої' in result
