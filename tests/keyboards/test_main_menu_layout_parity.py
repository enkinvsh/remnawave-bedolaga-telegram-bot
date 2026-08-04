"""Паритет главного меню: конструктор (`MENU_LAYOUT_ENABLED=True`) против текущего меню.

ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ.
`get_main_menu_keyboard_async` — единственный шов между двумя реализациями главного
меню: при `MENU_LAYOUT_ENABLED=True` клавиатуру собирает `MenuLayoutService` из
конфигурации в БД, иначе — хардкод `get_main_menu_keyboard`. Флаг переключается
глобально, сразу для ВСЕХ пользователей и ВСЕХ тенантов white-label продукта:
поэтапной раскатки нет. Значит единственная защита от «у людей внезапно поехали
кнопки» — доказательство, что при пустой БД (в проде строки `menu_layout_config`
нет, конфигурация берётся из `get_default_config()`) обе ветки рендерят
СТРУКТУРНО ОДИНАКОВУЮ клавиатуру.

ЧТО ЭТОТ ФАЙЛ СТОРОЖИТ.
Каждый тест ниже вызывает ОДНУ И ТУ ЖЕ функцию с ОДИНАКОВЫМИ аргументами, меняя
только `settings.MENU_LAYOUT_ENABLED`, и сравнивает результат структурно:
последовательность рядов, а внутри ряда — упорядоченный список кнопок по
`text` / `callback_data` / `url` / `web_app.url` / `icon_custom_emoji_id`.
Перенос кнопки в соседний ряд — такое же расхождение, как её пропажа.

ТЕКУЩИЙ СТАТУС: ПАРИТЕТ ДОСТИГНУТ, `xfail` НЕ ОСТАЛОСЬ.
Подписи берутся из слоя локализации по `text_key`
(`app/services/menu_layout/constants.py`), а структура воспроизведена тем, что
дефолтная конфигурация повторяет модель легаси-меню: три «прибитых» ряда
(connect / happ / баланс) плюс ОДИН ряд-поток `main_row` со всеми остальными
кнопками и `max_per_row=2`. `build_keyboard` режет по `max_per_row` уже видимые
кнопки, поэтому пары «переплывают» при скрытии кнопки ровно как в легаси.

Каждый тест здесь — утверждение о ПРОДЕ: включение флага не должно двигать ни одной
кнопки ни у одного тенанта. Если тест упал, менять надо конструктор, а не тест:
легаси-ветка тут по определению эталон, потому что это то, что люди видят сейчас.

Граблю знать обязательно: `MenuLayoutService._cache` — КЛАССОВЫЙ глобал, поэтому
фикстура инвалидирует его до и после каждого теста, иначе конфигурация протекает
между тестами и результаты сравнения становятся ложными.

ЧЕГО ЭТОТ ФАЙЛ НЕ ПОКРЫВАЕТ: `CONNECT_BUTTON_MODE`. Легаси рисует «Подключиться»
по режиму подключения (`web_app` / `url` / callback), а конструктор — по
`open_mode` кнопки, и на дефолте `open_mode='callback'` это расходится у
подписчика с рабочей ссылкой. Менять это здесь нельзя: `open_mode='callback'`
зафиксирован как «слать callback_data» в `tests/services/test_menu_layout_service.py`.
"""

from typing import Any

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.keyboards.inline import get_main_menu_keyboard_async
from app.services.menu_layout.service import MenuLayoutService
from tests.fixtures.menu_layout_db import FakeSettingsStoreDB, menu_layout_default_config


class _FakeSubscription:
    """Подписка ровно с теми атрибутами, которые читают обе ветки меню.

    `subscription_url` по умолчанию ПУСТОЙ — это состояние «ссылки ещё нет», в
    котором все режимы `CONNECT_BUTTON_MODE` сходятся на callback-кнопке. Именно
    поэтому расхождение по форме «Подключиться» долго пряталось: подписчику с
    рабочей ссылкой нужен явный `subscription_url` (см. `_SUBSCRIBER_WITH_LINK`).
    """

    def __init__(
        self,
        *,
        is_trial: bool = False,
        traffic_limit_gb: int = 0,
        tariff_id: int | None = None,
        subscription_url: str = '',
        subscription_crypto_link: str = '',
    ) -> None:
        self.is_trial = is_trial
        self.traffic_limit_gb = traffic_limit_gb
        self.tariff_id = tariff_id
        self.traffic_used_gb = 0.0
        self.days_left = 30
        self.autopay_enabled = False
        self.is_active = True
        self.subscription_url = subscription_url
        self.subscription_crypto_link = subscription_crypto_link


@pytest.fixture
def db() -> Any:
    """Пустое хранилище настроек + сброс классового кеша до и после теста."""
    with menu_layout_default_config() as fake_db:
        yield fake_db


# ---- Структурный снимок клавиатуры ---------------------------------------------

ButtonSnapshot = tuple[str, str | None, str | None, str | None, str | None]


def _snapshot(markup: InlineKeyboardMarkup) -> list[list[ButtonSnapshot]]:
    """Структурный снимок: ряды -> упорядоченные кнопки по значимым полям."""
    return [
        [
            (
                button.text,
                button.callback_data,
                button.url,
                button.web_app.url if button.web_app else None,
                getattr(button, 'icon_custom_emoji_id', None),
            )
            for button in row
        ]
        for row in markup.inline_keyboard
    ]


def _format(snapshot: list[list[ButtonSnapshot]]) -> str:
    """Читаемый рендер снимка — чтобы assert показывал расхождение построчно."""
    if not snapshot:
        return '<пустая клавиатура>'
    lines = []
    for index, row in enumerate(snapshot):
        cells = []
        for text, callback_data, url, web_app_url, custom_emoji_id in row:
            target = callback_data or url or web_app_url or '<нет действия>'
            suffix = f' emoji={custom_emoji_id}' if custom_emoji_id else ''
            cells.append(f'{text!r} -> {target}{suffix}')
        lines.append(f'ряд{index}: ' + ' | '.join(cells))
    return '\n'.join(lines)


async def _assert_parity(
    db: FakeSettingsStoreDB,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    **kwargs: Any,
) -> None:
    """Отрендерить обе ветки с одинаковыми аргументами и сравнить структурно."""
    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', False)
    MenuLayoutService.invalidate_cache()
    legacy = _snapshot(await get_main_menu_keyboard_async(db, language=language, **kwargs))

    monkeypatch.setattr(settings, 'MENU_LAYOUT_ENABLED', True)
    MenuLayoutService.invalidate_cache()
    layout = _snapshot(await get_main_menu_keyboard_async(db, language=language, **kwargs))

    assert layout == legacy, (
        f'Конструктор меню рендерит не то же самое, что текущее меню [{language}].\n'
        f'--- текущее меню (флаг ВЫКЛ) ---\n{_format(legacy)}\n'
        f'--- конструктор (флаг ВКЛ) ---\n{_format(layout)}'
    )


# ---- Матрица сценариев ---------------------------------------------------------
#
# Сценарии подобраны так, чтобы менялся НАБОР показываемых кнопок: именно смена
# набора вскрывает разницу в группировке, потому что в легаси-меню спрятанная
# кнопка не оставляет дыру, а подтягивает следующую.

SCENARIOS = [
    pytest.param('ru', {}, id='new_user-ru'),
    pytest.param('en', {}, id='new_user-en'),
    pytest.param(
        'ru',
        {
            'has_active_subscription': True,
            'subscription_is_active': True,
            'subscription': _FakeSubscription(is_trial=True),
        },
        id='active_trial-ru',
    ),
    pytest.param(
        'ru',
        {
            'has_active_subscription': True,
            'subscription_is_active': True,
            'has_had_paid_subscription': True,
            'subscription': _FakeSubscription(traffic_limit_gb=100),
        },
        id='active_paid-ru',
    ),
    pytest.param(
        'en',
        {
            'has_active_subscription': True,
            'subscription_is_active': True,
            'has_had_paid_subscription': True,
            'subscription': _FakeSubscription(traffic_limit_gb=100),
        },
        id='active_paid-en',
    ),
    pytest.param(
        'ru',
        {
            'has_active_subscription': True,
            'subscription_is_active': True,
            'has_had_paid_subscription': True,
            'subscription': _FakeSubscription(traffic_limit_gb=100, tariff_id=7),
        },
        id='active_paid_with_tariff-ru',
    ),
    pytest.param(
        'ru',
        {
            'has_active_subscription': True,
            'subscription_is_active': True,
            'has_had_paid_subscription': True,
            'subscription': _FakeSubscription(traffic_limit_gb=0),
        },
        id='active_paid_unlimited-ru',
    ),
    pytest.param(
        'ru',
        {
            'has_active_subscription': True,
            'subscription_is_active': False,
            'has_had_paid_subscription': True,
            'subscription': _FakeSubscription(traffic_limit_gb=100),
        },
        id='subscription_suspended-ru',
    ),
    pytest.param('ru', {'has_had_paid_subscription': True}, id='expired_paid-ru'),
    # Ненулевой баланс — единственный случай, когда легаси берёт `BALANCE_BUTTON`
    # вместо `BALANCE_BUTTON_DEFAULT`, поэтому проверяется и вне ru.
    pytest.param('ru', {'balance_kopeks': 150000}, id='balance_positive-ru'),
    pytest.param('en', {'balance_kopeks': 150000}, id='balance_positive-en'),
    pytest.param('fa', {'balance_kopeks': 150000}, id='balance_positive-fa'),
    pytest.param('ru', {'has_saved_cart': True, 'show_resume_checkout': True}, id='saved_cart-ru'),
    pytest.param('ru', {'is_admin': True}, id='admin-ru'),
    pytest.param('ru', {'is_moderator': True}, id='moderator-ru'),
    pytest.param('ru', {'is_admin': True, 'is_moderator': True}, id='admin_and_moderator-ru'),
]


@pytest.mark.parametrize(('language', 'kwargs'), SCENARIOS)
async def test_main_menu_parity(db, monkeypatch, language: str, kwargs: dict[str, Any]) -> None:
    """Обе ветки шва обязаны рендерить одинаковую клавиатуру для одного контекста."""
    await _assert_parity(db, monkeypatch, language, **kwargs)


@pytest.mark.parametrize('language', ['fa', 'zh', 'ua'])
async def test_main_menu_parity_for_non_ru_en_locales(db, monkeypatch, language: str) -> None:
    """Локали без словаря в конфигурации не должны молча становиться английскими."""
    await _assert_parity(db, monkeypatch, language)


async def test_main_menu_parity_keeps_custom_buttons(db, monkeypatch) -> None:
    """Кнопки из параметра custom_buttons обязаны доезжать до клавиатуры и на своё место."""
    await _assert_parity(
        db,
        monkeypatch,
        'ru',
        custom_buttons=[InlineKeyboardButton(text='🎉 Акция', callback_data='promo_action')],
    )


# ---- Оси настроек тенанта ------------------------------------------------------
#
# Продукт white-label: флаг переключается разом у всех тенантов, а настройки у них
# РАЗНЫЕ. Дефолты этого развёртывания ничего не доказывают про чужое, поэтому
# каждая настройка, влияющая на набор кнопок, проверяется в обоих положениях.
# `_FakeSubscription.subscription_url` пустой, поэтому CONNECT_BUTTON_MODE тут не
# участвует: все режимы подключения без ссылки сходятся на callback-кнопке.

_SUBSCRIBER_WITH_TRAFFIC = {
    'has_active_subscription': True,
    'subscription_is_active': True,
    'has_had_paid_subscription': True,
    'subscription': _FakeSubscription(traffic_limit_gb=100),
}

# Подписчик с РАБОЧЕЙ ссылкой: только на нём видно форму кнопки «Подключиться».
_SUBSCRIPTION_URL = 'https://panel.example.com/sub/abcdef'
_CRYPTO_LINK = 'happ://crypto/abcdef'
_CUSTOM_MINIAPP_URL = 'https://miniapp.example.com/app'

_SUBSCRIBER_WITH_LINK = {
    'has_active_subscription': True,
    'subscription_is_active': True,
    'has_had_paid_subscription': True,
    'subscription': _FakeSubscription(
        traffic_limit_gb=100,
        subscription_url=_SUBSCRIPTION_URL,
        subscription_crypto_link=_CRYPTO_LINK,
    ),
}

SETTINGS_AXES = [
    pytest.param({'MAIN_MENU_MODE': 'cabinet'}, {}, id='cabinet_mode'),
    pytest.param({'MAIN_MENU_MODE': 'cabinet'}, {'is_admin': True}, id='cabinet_mode-admin'),
    pytest.param({'ACTIVATE_BUTTON_VISIBLE': True}, {}, id='activate_button_on'),
    pytest.param(
        {'ACTIVATE_BUTTON_VISIBLE': True, 'ACTIVATE_BUTTON_TEXT': '🔑 Ввести ключ'},
        {},
        id='activate_button_custom_text',
    ),
    pytest.param({'ACTIVATE_BUTTON_VISIBLE': False}, {}, id='activate_button_off'),
    pytest.param({'CONTESTS_ENABLED': False, 'CONTESTS_BUTTON_VISIBLE': True}, {}, id='contests_master_switch_off'),
    pytest.param({'CONTESTS_ENABLED': True, 'CONTESTS_BUTTON_VISIBLE': True}, {}, id='contests_on'),
    pytest.param({'CONTESTS_ENABLED': True, 'CONTESTS_BUTTON_VISIBLE': False}, {}, id='contests_button_hidden'),
    pytest.param({'SIMPLE_SUBSCRIPTION_ENABLED': True}, {}, id='simple_subscription_on'),
    pytest.param(
        {'SIMPLE_SUBSCRIPTION_ENABLED': True},
        _SUBSCRIBER_WITH_TRAFFIC,
        id='simple_subscription_on-subscriber',
    ),
    pytest.param({'SALES_MODE': 'classic'}, _SUBSCRIBER_WITH_TRAFFIC, id='classic_mode-subscriber'),
    pytest.param({'SALES_MODE': 'tariffs'}, _SUBSCRIBER_WITH_TRAFFIC, id='tariffs_mode-subscriber'),
    pytest.param(
        {'SALES_MODE': 'classic', 'TRAFFIC_TOPUP_ENABLED': False},
        _SUBSCRIBER_WITH_TRAFFIC,
        id='traffic_topup_disabled',
    ),
    pytest.param(
        {'SALES_MODE': 'classic', 'TRAFFIC_SELECTION_MODE': 'fixed'},
        _SUBSCRIBER_WITH_TRAFFIC,
        id='traffic_topup_blocked',
    ),
    pytest.param({'BUY_TRAFFIC_BUTTON_VISIBLE': False}, _SUBSCRIBER_WITH_TRAFFIC, id='buy_traffic_button_hidden'),
    pytest.param({'TRIAL_DURATION_DAYS': 0}, {}, id='trial_disabled_by_duration'),
    pytest.param({'TRIAL_DISABLED_FOR': 'all'}, {}, id='trial_disabled_for_all'),
    pytest.param({'REFERRAL_PROGRAM_ENABLED': False}, {}, id='referrals_off'),
    pytest.param({'SUPPORT_MENU_ENABLED': False}, {}, id='support_off'),
    pytest.param({'LANGUAGE_SELECTION_ENABLED': False}, {}, id='language_selection_off'),
    pytest.param(
        {'CONNECT_BUTTON_MODE': 'happ_cryptolink', 'CONNECT_BUTTON_HAPP_DOWNLOAD_ENABLED': True},
        _SUBSCRIBER_WITH_TRAFFIC,
        id='happ_download_row',
    ),
    # --- CONNECT_BUTTON_MODE: форма главной кнопки бота ---
    #
    # Проверяется у подписчика С РАБОЧЕЙ ССЫЛКОЙ: без неё все режимы сходятся на
    # callback и расхождение не видно. `_assert_parity` сравнивает web_app.url /
    # url / callback_data, поэтому подмена Mini App на callback здесь падает.
    pytest.param({'CONNECT_BUTTON_MODE': 'miniapp_subscription'}, _SUBSCRIBER_WITH_LINK, id='connect-miniapp_sub'),
    pytest.param({'CONNECT_BUTTON_MODE': 'link'}, _SUBSCRIBER_WITH_LINK, id='connect-link'),
    pytest.param(
        {'CONNECT_BUTTON_MODE': 'miniapp_custom', 'MINIAPP_CUSTOM_URL': _CUSTOM_MINIAPP_URL},
        _SUBSCRIBER_WITH_LINK,
        id='connect-miniapp_custom',
    ),
    pytest.param(
        {'CONNECT_BUTTON_MODE': 'happ_cryptolink'},
        _SUBSCRIBER_WITH_LINK,
        id='connect-happ_cryptolink',
    ),
    pytest.param(
        {'CONNECT_BUTTON_MODE': 'happ_cryptolink', 'MULTI_TARIFF_ENABLED': True, 'SALES_MODE': 'tariffs'},
        _SUBSCRIBER_WITH_LINK,
        id='connect-happ_cryptolink-multi_tariff',
    ),
    pytest.param({'CONNECT_BUTTON_MODE': 'miniapp_subscription'}, _SUBSCRIBER_WITH_TRAFFIC, id='connect-sub-fallback'),
    pytest.param({'CONNECT_BUTTON_MODE': 'link'}, _SUBSCRIBER_WITH_TRAFFIC, id='connect-link-fallback'),
    pytest.param(
        {'CONNECT_BUTTON_MODE': 'miniapp_custom', 'MINIAPP_CUSTOM_URL': _CUSTOM_MINIAPP_URL},
        _SUBSCRIBER_WITH_TRAFFIC,
        id='connect-miniapp_custom-without-link',
    ),
    pytest.param({'CONNECT_BUTTON_MODE': 'totally_unknown'}, _SUBSCRIBER_WITH_LINK, id='connect-unknown-mode'),
    # --- Мультитариф: подпись кнопки подписки ---
    pytest.param(
        {'MULTI_TARIFF_ENABLED': True, 'SALES_MODE': 'tariffs'},
        _SUBSCRIBER_WITH_LINK,
        id='multi_tariff-subscriber',
    ),
    pytest.param({'MULTI_TARIFF_ENABLED': True, 'SALES_MODE': 'tariffs'}, {}, id='multi_tariff-new_user'),
]


@pytest.mark.parametrize('language', ['ru', 'fa'])
@pytest.mark.parametrize(('overrides', 'kwargs'), SETTINGS_AXES)
async def test_main_menu_parity_across_settings(
    db,
    monkeypatch,
    overrides: dict[str, Any],
    kwargs: dict[str, Any],
    language: str,
) -> None:
    """Паритет обязан держаться при любых настройках тенанта, а не только на дефолтных.

    Прогон идёт и на `fa`: подписи легаси берёт то атрибутом (`texts.MENU_TRIAL`), то
    методом (`texts.t(...)` с литеральным дефолтом), а конструктор — всегда
    `texts.get(text_key)`. Ключ, которого нет в локали, эти пути разводят, и увидеть
    это можно только на языке помимо ru/en.
    """
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    await _assert_parity(db, monkeypatch, language, **kwargs)
