"""Declarative registry of previewable bot screens.

A screen is "what the user sees in one message". The locale editor lists these
so the owner can pick a screen and edit exactly the strings it is made of,
instead of hunting through ~2000 flat keys.

Adding a screen is three things: an ``async def _render_x(texts, db, state) -> str``
that delegates to the **real** handler (never a copy of its formatting — a copy
drifts), the list of ``keys`` it can use, and one :func:`register_screen` call
below.

Why ``keys`` is declared by hand instead of being derived from tracing: tracing
only ever sees the strings the *current* state renders. Some strings are
unreachable from a synthetic user by construction — ``MAIN_MENU_TARIFF_LINE``
needs a tariff row from the database, the ``SUB_MULTI_*`` suffixes need
multi-tariff mode. Reaching them would mean faking the DB or patching settings
per request, which is not safe under concurrency. Declaring them keeps them
editable; the preview marks them ``rendered: false``. A test walks every state
and fails if a screen renders a key nobody declared.

The handler is imported lazily inside the renderer: ``app.handlers.menu`` pulls
in aiogram and half the service layer, and the cabinet must not pay for that on
import.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .synthetic import DEFAULT_SYNTHETIC_STATE, SYNTHETIC_STATES, SyntheticState


if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from app.localization.texts import Texts


# (texts, db, state) -> rendered message text
ScreenRenderer = Callable[['Texts', Any, str], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ScreenDefinition:
    """One previewable screen.

    ``keys`` is every localization key the screen can use — not merely the ones
    a particular state happens to render.
    """

    id: str
    title: str
    description: str
    render: ScreenRenderer
    keys: tuple[str, ...] = ()
    states: tuple[SyntheticState, ...] = SYNTHETIC_STATES
    default_state: str = DEFAULT_SYNTHETIC_STATE


_SCREENS: dict[str, ScreenDefinition] = {}


def register_screen(screen: ScreenDefinition) -> ScreenDefinition:
    """Add ``screen`` to the registry, replacing a same-id entry."""
    _SCREENS[screen.id] = screen
    return screen


def get_screen(screen_id: str) -> ScreenDefinition | None:
    return _SCREENS.get(screen_id)


def list_screens() -> list[ScreenDefinition]:
    """Registered screens, in registration order."""
    return list(_SCREENS.values())


# ============ Screens ============


MAIN_MENU_KEYS: tuple[str, ...] = (
    'MAIN_MENU',
    'MAIN_MENU_ACTION_PROMPT',
    # Требует тариф из БД — синтетическим пользователем недостижима.
    'MAIN_MENU_TARIFF_LINE',
    # Ветки _get_subscription_status (одно-тарифный режим).
    'SUB_STATUS_NONE',
    'SUBSCRIPTION_NONE',
    'SUB_STATUS_ACTIVE_LONG',
    'SUB_STATUS_ACTIVE_FEW_DAYS',
    'SUB_STATUS_ACTIVE_TOMORROW',
    'SUB_STATUS_ACTIVE_TODAY',
    'SUB_STATUS_DAILY_ACTIVE',
    'SUB_STATUS_TRIAL_ACTIVE',
    'SUB_STATUS_TRIAL_TOMORROW',
    'SUB_STATUS_TRIAL_TODAY',
    'SUB_STATUS_EXPIRED',
    'SUB_STATUS_DISABLED',
    'SUB_STATUS_LIMITED',
    'SUB_STATUS_UNKNOWN',
    # Блоки, которые get_main_menu_text дописывает после статуса. Каждый читает
    # БД по конкретному пользователю (промо-предложение, тестовый доступ), так
    # что демо-пользователь их не рендерит — но на экране они бывают, и без
    # объявления оказались бы нередактируемыми вообще.
    'SUBSCRIPTION_PROMO_DISCOUNT_HINT',
    'MAIN_MENU_TEST_ACCESS_HEADER',
    'MAIN_MENU_TEST_ACCESS_TIMER',
    # Ветки _get_multi_tariff_status: живут за настройкой MULTI_TARIFF_ENABLED
    # и читают подписки из БД — тоже недостижимы.
    'SUB_MULTI_FALLBACK_NAME',
    'SUB_MULTI_SUFFIX_UNTIL',
    'SUB_MULTI_SUFFIX_EXPIRED',
    'SUB_MULTI_SUFFIX_DISABLED',
    'SUB_MULTI_SUFFIX_LIMITED',
)


async def _render_main_menu(texts: Texts, db: Any, state: str) -> str:
    """The main menu, rendered by the bot's own ``get_main_menu_text``.

    In multi-tariff mode the handler reads the subscription list straight from
    the DB by user id; the synthetic user has none, so the preview shows the
    "no subscriptions" variant of the block. Single-tariff mode (the default)
    reads ``user.subscription`` off the synthetic instance and shows the status
    line for the requested ``state``.
    """
    from app.handlers.menu import get_main_menu_text

    from .synthetic import attach_sample_tariff, build_synthetic_user

    user = build_synthetic_user(texts.language, state=state)
    # Без реального tariff_id строка тарифа не рендерится, и превью расходится
    # с ботом — владелец видел экран без «Тариф: …» и не мог отредактировать
    # эту строку осмысленно. Тариф берётся из БД только на чтение.
    await attach_sample_tariff(db, user)
    return await get_main_menu_text(user, texts, db)


register_screen(
    ScreenDefinition(
        id='main_menu',
        title='Главное меню',
        description='Первый экран после /start: имя пользователя, статус подписки и приглашение выбрать действие.',
        render=_render_main_menu,
        keys=MAIN_MENU_KEYS,
    )
)


BALANCE_KEYS: tuple[str, ...] = ('BALANCE_INFO',)

# Баланс не зависит от подписки: предлагать все девять состояний значило бы
# показывать владельцу выбор, который ничего не меняет. Id остаётся валидным
# ключом сборщика пользователя, а подпись говорит правду про этот экран.
BALANCE_STATES: tuple[SyntheticState, ...] = (SyntheticState(DEFAULT_SYNTHETIC_STATE, 'Демо-пользователь'),)


async def _render_balance(texts: Texts, db: Any, state: str) -> str:
    """Экран «Баланс» — той же функцией, которой его собирает хендлер."""
    from app.handlers.balance.main import get_balance_text

    from .synthetic import build_synthetic_user

    return get_balance_text(build_synthetic_user(texts.language, state=state), texts)


register_screen(
    ScreenDefinition(
        id='balance',
        title='Баланс',
        description='Экран пополнения: текущий баланс пользователя и приглашение выбрать действие.',
        render=_render_balance,
        keys=BALANCE_KEYS,
        states=BALANCE_STATES,
    )
)


# Экран кнопки «Подписка» собирается build_subscription_overview_text из
# purchase.py. Раньше сюда был подключён get_subscription_info_text из
# pricing.py с шаблоном SUBSCRIPTION_INFO — владелец правил ключ, который
# реальный экран не читает, и правка «не срабатывала».
SUBSCRIPTION_KEYS: tuple[str, ...] = (
    'SUBSCRIPTION_OVERVIEW_TEMPLATE',
    # Шаблон для суточных тарифов: нужен тариф с is_daily из БД.
    'SUBSCRIPTION_DAILY_OVERVIEW_TEMPLATE',
    # Экран без подписки вообще.
    'SUBSCRIPTION_NONE',
    # Статус подписки.
    'SUBSCRIPTION_STATUS_ACTIVE',
    'SUBSCRIPTION_STATUS_TRIAL',
    'SUBSCRIPTION_STATUS_EXPIRED',
    'SUBSCRIPTION_STATUS_LIMITED',
    'SUBSCRIPTION_STATUS_DISABLED',
    'SUBSCRIPTION_STATUS_UNKNOWN',
    # Остаток срока и предупреждения.
    'SUBSCRIPTION_TIME_LEFT_DAYS',
    'SUBSCRIPTION_TIME_LEFT_HOURS',
    'SUBSCRIPTION_TIME_LEFT_MINUTES',
    'SUBSCRIPTION_TIME_LEFT_EXPIRED',
    'SUBSCRIPTION_WARNING_TOMORROW',
    'SUBSCRIPTION_WARNING_TODAY',
    'SUBSCRIPTION_WARNING_MINUTES',
    # Тип и трафик.
    'SUBSCRIPTION_TYPE_TRIAL',
    'SUBSCRIPTION_TYPE_PAID',
    'SUBSCRIPTION_TRAFFIC_LIMITED',
    'SUBSCRIPTION_TRAFFIC_UNLIMITED',
    'SUBSCRIPTION_NO_SERVERS',
    # Блок тарифа: рендерится только в режиме тарифов и только при tariff_id.
    'SUBSCRIPTION_TARIFF_NAME_LINE',
    'SUBSCRIPTION_TARIFF_TYPE_LINE',
    'SUBSCRIPTION_TARIFF_TYPE_DAILY',
    'SUBSCRIPTION_TARIFF_TYPE_PERIODIC',
    'SUBSCRIPTION_TARIFF_TRAFFIC_LINE',
    'SUBSCRIPTION_TARIFF_TRAFFIC_UNLIMITED_LINE',
    'SUBSCRIPTION_TARIFF_DEVICES_LINE',
    'SUBSCRIPTION_TARIFF_DAILY_PRICE_LINE',
    'SUBSCRIPTION_TARIFF_DAILY_PAUSED',
    'SUBSCRIPTION_TARIFF_DAILY_TIME_LEFT',
    'SUBSCRIPTION_TARIFF_DAILY_CHARGE_PAUSED',
    'SUBSCRIPTION_TARIFF_DAILY_NEXT_CHARGE',
    'SUBSCRIPTION_TARIFF_DAILY_PROGRESS',
    'SUBSCRIPTION_TARIFF_DAILY_FIRST_CHARGE',
    # Список устройств приходит из панели по uuid, которого у демо-юзера нет.
    'SUBSCRIPTION_CONNECTED_DEVICES_TITLE',
    'SUBSCRIPTION_CONNECTED_DEVICES_FOOTER',
    'SUBSCRIPTION_DEVICE_LINE',
    'SUBSCRIPTION_DEVICE_ITEM',
    'SUBSCRIPTION_DEVICE_UNKNOWN',
    # Докупленный трафик: TrafficPurchase читается из БД по id подписки.
    'SUBSCRIPTION_PURCHASED_TRAFFIC_TITLE',
    'SUBSCRIPTION_PURCHASED_TRAFFIC_FOOTER',
    'SUBSCRIPTION_PURCHASED_TRAFFIC_ITEM',
    'SUBSCRIPTION_PURCHASED_TRAFFIC_PROGRESS',
    'SUBSCRIPTION_PURCHASED_EXPIRES_TODAY',
    'SUBSCRIPTION_PURCHASED_ONE_DAY_LEFT',
    'SUBSCRIPTION_PURCHASED_FEW_DAYS_LEFT',
    'SUBSCRIPTION_PURCHASED_MANY_DAYS_LEFT',
    # Ссылка подключения приходит из панели.
    'SUBSCRIPTION_CONNECT_LINK_SECTION',
    'SUBSCRIPTION_CONNECT_LINK_PROMPT',
)

# Экран показывает КОНКРЕТНУЮ подписку, поэтому состояния без неё ('none') не
# предлагаем. В отличие от главного меню, здесь disabled и limited дают СВОИ
# строки статуса, так что оба состояния осмысленны.
SUBSCRIPTION_STATES: tuple[SyntheticState, ...] = tuple(state for state in SYNTHETIC_STATES if state.id != 'none')


async def _render_subscription(texts: Texts, db: Any, state: str) -> str:
    """Экран «Подписка» — тем же билдером, что зовёт хендлер menu_subscription."""
    from app.handlers.subscription.purchase import build_subscription_overview_text

    from .synthetic import attach_sample_tariff, build_synthetic_user

    user = build_synthetic_user(texts.language, state=state)
    await attach_sample_tariff(db, user)
    return await build_subscription_overview_text(user, texts, db)


register_screen(
    ScreenDefinition(
        id='subscription',
        title='Подписка',
        description='Экран кнопки «Подписка»: баланс, статус, срок, трафик, серверы, устройства и ссылка подключения.',
        render=_render_subscription,
        keys=SUBSCRIPTION_KEYS,
        states=SUBSCRIPTION_STATES,
    )
)


SUPPORT_KEYS: tuple[str, ...] = ('SUPPORT_INFO',)

# Текст поддержки не зависит от подписки — предлагать девять состояний значило бы
# показывать владельцу выбор, который ничего не меняет.
SUPPORT_STATES: tuple[SyntheticState, ...] = (SyntheticState(DEFAULT_SYNTHETIC_STATE, 'Демо-пользователь'),)


async def _render_support(texts: Texts, db: Any, state: str) -> str:
    """Экран «Поддержка» — та же строка, что уходит в caption хендлера menu_support."""
    return texts.SUPPORT_INFO


register_screen(
    ScreenDefinition(
        id='support',
        title='Поддержка',
        description='Экран кнопки «Техподдержка»: как связаться с поддержкой и с чем она помогает.',
        render=_render_support,
        keys=SUPPORT_KEYS,
        states=SUPPORT_STATES,
    )
)


# Ни один из четырёх экранов ниже не зависит от подписки, поэтому все они берут
# одно состояние: девять вариантов в выпадающем списке ничего бы не меняли.
STATELESS_STATES: tuple[SyntheticState, ...] = (SyntheticState(DEFAULT_SYNTHETIC_STATE, 'Демо-пользователь'),)


# Экран кнопки menu_promocode рисует show_promocode_menu из promocode.py, и
# рисует он ровно один ключ — PROMOCODE_ENTER уходит в edit_text без сборки.
PROMOCODE_KEYS: tuple[str, ...] = ('PROMOCODE_ENTER',)


async def _render_promocode(texts: Texts, db: Any, state: str) -> str:
    """Экран «Промокод» — та же строка, что уходит в edit_text хендлера menu_promocode."""
    return texts.PROMOCODE_ENTER


register_screen(
    ScreenDefinition(
        id='promocode',
        title='Промокод',
        description='Экран кнопки «Промокод»: приглашение ввести код.',
        render=_render_promocode,
        keys=PROMOCODE_KEYS,
        states=STATELESS_STATES,
    )
)


# Экран кнопки menu_referrals собирает build_referral_info_text из referral.py.
REFERRAL_KEYS: tuple[str, ...] = (
    'REFERRAL_PROGRAM_TITLE',
    'REFERRAL_STATS_HEADER',
    'REFERRAL_STATS_INVITED',
    'REFERRAL_STATS_FIRST_TOPUPS',
    'REFERRAL_STATS_ACTIVE',
    'REFERRAL_STATS_CONVERSION',
    'REFERRAL_STATS_TOTAL_EARNED',
    'REFERRAL_STATS_MONTH_EARNED',
    'REFERRAL_REWARDS_HEADER',
    # Строки наград живут за настройками сумм: при нулевом бонусе бот их не
    # печатает вовсе, но на боевой конфигурации они видны.
    'REFERRAL_REWARD_NEW_USER',
    'REFERRAL_REWARD_INVITER',
    # Взаимоисключающие ветки: лимит на число платежей с комиссией задан или нет.
    'REFERRAL_REWARD_COMMISSION_LIMITED',
    'REFERRAL_REWARD_COMMISSION',
    'REFERRAL_BOT_LINK_TITLE',
    # Ссылка на кабинет печатается только когда CABINET_URL настроен.
    'REFERRAL_CABINET_LINK_TITLE',
    'REFERRAL_CODE_TITLE',
    # Блок последних начислений: ReferralEarning читается из БД по id
    # пользователя, у демо-пользователя таких строк нет.
    'REFERRAL_RECENT_EARNINGS_HEADER',
    'REFERRAL_RECENT_EARNINGS_ITEM',
    'REFERRAL_EARNING_REASON_FIRST_TOPUP',
    'REFERRAL_EARNING_REASON_COMMISSION_TOPUP',
    'REFERRAL_EARNING_REASON_COMMISSION_PURCHASE',
    # Блок «доходы по типам» — та же агрегация по БД, тоже недостижим.
    'REFERRAL_EARNINGS_BY_TYPE_HEADER',
    'REFERRAL_EARNINGS_FIRST_TOPUPS',
    'REFERRAL_EARNINGS_TOPUPS',
    'REFERRAL_EARNINGS_PURCHASES',
    'REFERRAL_INVITE_FOOTER',
)


async def _render_referral(texts: Texts, db: Any, state: str) -> str:
    """Экран «Партнерка» — тем же билдером, что зовёт хендлер menu_referrals.

    ``bot_username`` не передаём: хендлер берёт его из ``callback.bot.get_me()``,
    а без него ссылка собирается по username из настроек — ровно та же строка.
    """
    from app.handlers.referral import build_referral_info_text

    from .synthetic import build_synthetic_user

    return await build_referral_info_text(build_synthetic_user(texts.language, state=state), texts, db)


register_screen(
    ScreenDefinition(
        id='referral',
        title='Партнерка',
        description='Экран кнопки «Партнёрская программа»: статистика приглашений, награды, ссылки и код.',
        render=_render_referral,
        keys=REFERRAL_KEYS,
        states=STATELESS_STATES,
    )
)


# Экран кнопки menu_info рисует show_info_menu из menu.py: подпись — заголовок и
# подсказка, всё остальное на этом экране живёт в кнопках, а не в тексте.
INFO_KEYS: tuple[str, ...] = ('MENU_INFO_HEADER', 'MENU_INFO_PROMPT')


async def _render_info(texts: Texts, db: Any, state: str) -> str:
    """Экран «Инфо» — той же функцией, которой подпись собирает хендлер."""
    from app.handlers.menu import build_info_menu_caption

    return build_info_menu_caption(texts)


register_screen(
    ScreenDefinition(
        id='info',
        title='Инфо',
        description='Экран кнопки «Инфо»: заголовок раздела и приглашение выбрать подраздел.',
        render=_render_info,
        keys=INFO_KEYS,
        states=STATELESS_STATES,
    )
)


# Экран кнопки menu_language рисует show_language_menu из menu.py — один ключ в
# caption. Ветки «пользователь не найден» и «выбор языка выключен» отвечают
# алертом (callback.answer), то есть экраном не являются и сюда не входят.
LANGUAGE_KEYS: tuple[str, ...] = ('LANGUAGE_PROMPT',)


async def _render_language(texts: Texts, db: Any, state: str) -> str:
    """Экран «Язык» — та же строка, что уходит в caption хендлера menu_language."""
    return texts.t('LANGUAGE_PROMPT', '🌐 Выберите язык интерфейса:')


register_screen(
    ScreenDefinition(
        id='language',
        title='Язык',
        description='Экран кнопки «Язык»: приглашение выбрать язык интерфейса.',
        render=_render_language,
        keys=LANGUAGE_KEYS,
        states=STATELESS_STATES,
    )
)
