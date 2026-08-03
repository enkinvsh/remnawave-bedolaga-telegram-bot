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
