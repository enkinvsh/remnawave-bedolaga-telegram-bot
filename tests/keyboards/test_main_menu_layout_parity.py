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

ТЕКУЩИЙ СТАТУС: ПАРИТЕТА НЕТ НИ В ОДНОМ СЦЕНАРИИ.
Поэтому все тесты помечены `xfail(strict=True)`, а в `reason` записано конкретное
расхождение. Это НЕ фиксация расхождения как правильного поведения: как только
кто-то починит паритет, strict-xfail упадёт с XPASS и заставит снять маркер.
До тех пор файл документирует, что флаг включать нельзя.

Граблю знать обязательно: `MenuLayoutService._cache` — КЛАССОВЫЙ глобал, поэтому
фикстура инвалидирует его до и после каждого теста, иначе конфигурация протекает
между тестами и результаты сравнения становятся ложными.
"""

from typing import Any

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import settings
from app.keyboards.inline import get_main_menu_keyboard_async
from app.services.menu_layout.service import MenuLayoutService


# ---- Фейковая сессия -----------------------------------------------------------
#
# Прод не хранит строку `menu_layout_config`, поэтому конфигурация приходит из
# `get_default_config()`. Пустое хранилище воспроизводит ровно это состояние.


class _FakeResult:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    def scalar_one_or_none(self) -> Any:
        return self._obj


class _FakeDB:
    """Минимальная замена AsyncSession для `select(SystemSetting).where(key == ...)`."""

    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    async def execute(self, statement):
        key = statement.whereclause.right.value
        return _FakeResult(self.store.get(key))

    def add(self, obj) -> None:
        self.store[obj.key] = obj

    async def flush(self) -> None:
        """Фейковая сессия ничего не сбрасывает на диск."""

    async def commit(self) -> None:
        """Фейковая сессия ничего не коммитит."""


class _FakeSubscription:
    """Подписка ровно с теми атрибутами, которые читают обе ветки меню."""

    def __init__(self, *, is_trial: bool = False, traffic_limit_gb: int = 0) -> None:
        self.is_trial = is_trial
        self.traffic_limit_gb = traffic_limit_gb
        self.traffic_used_gb = 0.0
        self.days_left = 30
        self.autopay_enabled = False
        self.is_active = True
        self.subscription_url = ''
        self.subscription_crypto_link = ''


@pytest.fixture
def db() -> Any:
    """Пустое хранилище настроек + сброс классового кеша до и после теста."""
    MenuLayoutService.invalidate_cache()
    yield _FakeDB({})
    MenuLayoutService.invalidate_cache()


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


async def _assert_parity(db: _FakeDB, monkeypatch: pytest.MonkeyPatch, language: str, **kwargs: Any) -> None:
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
# Сценарии подобраны так, чтобы менялся НАБОР показываемых кнопок. `reason` у
# каждого — конкретное расхождение, снятое с реального рендера обеих веток.

_LABELS_RU = (
    'подписи из конфигурации не совпадают с локалью: '
    "'🧪 Тестовая подписка'/'🎁 Пробный период', '💎 Купить подписку'/'🛒 Купить подписку', "
    "'🎫 Промокод'/'🎟️ Промокод', '🤝 Партнерка'/'👥 Рефералы', '🛠️ Техподдержка'/'💬 Поддержка'"
)

_LABELS_EN = (
    'подписи из конфигурации не совпадают с локалью: '
    "'🎁 Trial subscription'/'🎁 Free trial', '💎 Buy subscription'/'🛒 Buy subscription', "
    "'🎫 Promo code'/'🎟️ Promo code', '🤝 Referral program'/'👥 Referrals', '🛠️ Support'/'💬 Support'"
)

_ROW_SWAP = (
    'ряд «Баланс» переезжает с позиции 1 на позицию 2 — конструктор ставит '
    'ряд «Подписка» ПЕРЕД балансом, текущее меню — ПОСЛЕ'
)

SCENARIOS = [
    pytest.param(
        'ru',
        {},
        id='new_user-ru',
        marks=pytest.mark.xfail(strict=True, reason=f'Новый пользователь, ru: {_LABELS_RU}'),
    ),
    pytest.param(
        'en',
        {},
        id='new_user-en',
        marks=pytest.mark.xfail(strict=True, reason=f'Новый пользователь, en: {_LABELS_EN}'),
    ),
    pytest.param(
        'ru',
        {
            'has_active_subscription': True,
            'subscription_is_active': True,
            'subscription': _FakeSubscription(is_trial=True),
        },
        id='active_trial-ru',
        marks=pytest.mark.xfail(
            strict=True,
            reason=(
                f'Активный триал, ru: {_ROW_SWAP}; кроме того текущее меню сплющивает '
                'остаток кнопок в общий поток по 2 и даёт пары '
                '[Подписка, Промокод] / [Партнерка, Техподдержка] / [Инфо, Язык], '
                'а конструктор держит семантические ряды '
                '[Подписка] / [Промокод, Рефералы] / [Поддержка, Инфо] / [Язык]; '
                f'плюс {_LABELS_RU}'
            ),
        ),
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
        marks=pytest.mark.xfail(
            strict=True,
            reason=(
                f'Активная платная подписка, ru: {_ROW_SWAP}; при SALES_MODE=tariffs '
                "конструктор ВООБЩЕ не показывает '📈 Докупить трафик' (условие "
                'traffic_topup_enabled жёстко запрещает докупку в режиме тарифов), '
                f'а текущее меню её показывает; плюс {_LABELS_RU}'
            ),
        ),
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
        marks=pytest.mark.xfail(
            strict=True,
            reason=(
                f'Активная платная подписка, en: {_ROW_SWAP}; '
                "'📈 Buy more traffic' пропадает в режиме тарифов; "
                f"'📱 Subscription' против '📊 Subscription'; плюс {_LABELS_EN}"
            ),
        ),
    ),
    pytest.param(
        'ru',
        {'has_had_paid_subscription': True},
        id='expired_paid-ru',
        marks=pytest.mark.xfail(
            strict=True,
            reason=(
                'Истёкшая подписка (платил раньше), ru: текущее меню склеивает '
                '[Купить подписку, Промокод] / [Партнерка, Техподдержка] / [Инфо, Язык], '
                'конструктор даёт [Купить подписку] / [Промокод, Рефералы] / '
                f'[Поддержка, Инфо] / [Язык]; плюс {_LABELS_RU}'
            ),
        ),
    ),
    pytest.param(
        'ru',
        {'balance_kopeks': 150000},
        id='balance_positive-ru',
        marks=pytest.mark.xfail(
            strict=True,
            reason=(f"Ненулевой баланс, ru: сама кнопка баланса совпадает ('💰 Баланс: 1500 ₽'), но {_LABELS_RU}"),
        ),
    ),
    pytest.param(
        'ru',
        {'has_saved_cart': True, 'show_resume_checkout': True},
        id='saved_cart-ru',
        marks=pytest.mark.xfail(
            strict=True,
            reason=(
                'Сохранённая корзина, ru: конструктор ВОЗВРАЩАЕТ в главное меню кнопку '
                "'↩️ Вернуться к оформлению' -> return_to_saved_cart, которую из главного "
                'меню намеренно убрали и перенесли на экран «Баланс»; '
                f'плюс {_LABELS_RU}'
            ),
        ),
    ),
    pytest.param(
        'ru',
        {'is_admin': True},
        id='admin-ru',
        marks=pytest.mark.xfail(
            strict=True,
            reason=(f"Админ, ru: '⚙️ Админ-панель' против '⚙️ Админ панель' (нет дефиса); плюс {_LABELS_RU}"),
        ),
    ),
    pytest.param(
        'ru',
        {'is_moderator': True},
        id='moderator-ru',
        marks=pytest.mark.xfail(
            strict=True,
            reason=(f"Модератор, ru: сама кнопка '🧑\u200d⚖️ Модерация' совпадает, но {_LABELS_RU}"),
        ),
    ),
]


@pytest.mark.parametrize(('language', 'kwargs'), SCENARIOS)
async def test_main_menu_parity(db, monkeypatch, language: str, kwargs: dict[str, Any]) -> None:
    """Обе ветки шва обязаны рендерить одинаковую клавиатуру для одного контекста."""
    await _assert_parity(db, monkeypatch, language, **kwargs)


# ---- Расхождения, которые не видны на дефолтных настройках ----------------------
#
# Ниже — классы расхождений, зависящие от настроек тенанта. На дефолтах они не
# всплывают, но white-label клиенты с такими настройками существуют, и флип
# ударит по ним молча. Тоже xfail(strict=True): тест утверждает ЖЕЛАЕМОЕ
# поведение (паритет), а не фиксирует поломку.


@pytest.mark.parametrize('language', ['fa', 'zh', 'ua'])
@pytest.mark.xfail(
    strict=True,
    reason=(
        'DEFAULT_MENU_CONFIG содержит подписи только для ru и en, поэтому '
        '_get_localized_text откатывается на en: пользователь с локалью fa/zh/ua '
        'вместо своего языка увидит английские кнопки (Balance / Free trial / '
        'Buy subscription / Promo code / Referrals / Support / Info / Language)'
    ),
)
async def test_main_menu_parity_for_non_ru_en_locales(db, monkeypatch, language: str) -> None:
    """Локали без словаря в конфигурации не должны молча становиться английскими."""
    await _assert_parity(db, monkeypatch, language)


@pytest.mark.xfail(
    strict=True,
    reason=(
        'MenuLayoutService.build_keyboard игнорирует context.custom_buttons: '
        'кнопки, переданные вызывающим в custom_buttons, при включённом флаге '
        'исчезают из меню (сейчас ни один прод-вызов их не передаёт, поэтому '
        'расхождение спящее, но параметр в сигнатуре остаётся и обманывает)'
    ),
)
async def test_main_menu_parity_keeps_custom_buttons(db, monkeypatch) -> None:
    """Кнопки из параметра custom_buttons обязаны доезжать до клавиатуры."""
    await _assert_parity(
        db,
        monkeypatch,
        'ru',
        custom_buttons=[InlineKeyboardButton(text='🎉 Акция', callback_data='promo_action')],
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        'get_main_menu_keyboard_async проверяет MENU_LAYOUT_ENABLED ДО делегирования '
        'в get_main_menu_keyboard, а ветку MAIN_MENU_MODE=cabinet обрабатывает только '
        'синхронная версия. При включённом флаге кабинетное меню пропадает целиком: '
        "кнопки '👤 Личный кабинет' не будет, вместо неё нарисуется обычное меню"
    ),
)
async def test_main_menu_parity_in_cabinet_mode(db, monkeypatch) -> None:
    """Режим MAIN_MENU_MODE=cabinet не должен обходиться конструктором стороной."""
    monkeypatch.setattr(settings, 'MAIN_MENU_MODE', 'cabinet')
    await _assert_parity(db, monkeypatch, 'ru')


@pytest.mark.xfail(
    strict=True,
    reason=(
        'DEFAULT_MENU_CONFIG вообще не содержит кнопку активации: при '
        'ACTIVATE_BUTTON_VISIBLE=True текущее меню рисует ACTIVATE_BUTTON_TEXT -> '
        'activate_button, а конструктор — нет, кнопка молча пропадает'
    ),
)
async def test_main_menu_parity_keeps_activate_button(db, monkeypatch) -> None:
    """Кнопка активации, включённая настройкой, не должна исчезать при флипе."""
    monkeypatch.setattr(settings, 'ACTIVATE_BUTTON_VISIBLE', True)
    await _assert_parity(db, monkeypatch, 'ru')


@pytest.mark.xfail(
    strict=True,
    reason=(
        'Условие contests_visible в конструкторе смотрит только на '
        'CONTESTS_BUTTON_VISIBLE, а текущее меню требует ещё и CONTESTS_ENABLED: '
        "у тенанта с выключенными конкурсами кнопка '🎲 Конкурсы' -> contests_menu "
        'ПОЯВИТСЯ после флипа'
    ),
)
async def test_main_menu_parity_respects_contests_master_switch(db, monkeypatch) -> None:
    """Кнопка конкурсов не должна появляться при выключенных конкурсах."""
    monkeypatch.setattr(settings, 'CONTESTS_ENABLED', False)
    monkeypatch.setattr(settings, 'CONTESTS_BUTTON_VISIBLE', True)
    await _assert_parity(db, monkeypatch, 'ru')


@pytest.mark.xfail(
    strict=True,
    reason=(
        'Простая покупка: текущее меню кладёт кнопку в общий поток по 2 в паре с '
        "промокодом и подписывает её '⚡ Простая покупка' из локали, конструктор "
        "выносит её в отдельный ряд max_per_row=1 с подписью '💳 Простая подписка'"
    ),
)
async def test_main_menu_parity_with_simple_subscription(db, monkeypatch) -> None:
    """Кнопка простой покупки должна остаться на своём месте и со своей подписью."""
    monkeypatch.setattr(settings, 'SIMPLE_SUBSCRIPTION_ENABLED', True)
    await _assert_parity(db, monkeypatch, 'ru')
