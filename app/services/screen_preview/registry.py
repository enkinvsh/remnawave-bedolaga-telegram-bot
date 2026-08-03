"""Declarative registry of previewable bot screens.

A screen is "what the user sees in one message". The locale editor lists these
so the owner can pick a screen and edit exactly the strings it is made of,
instead of hunting through ~2000 flat keys.

Adding a screen is two things: an ``async def _render_x(texts, db) -> str`` that
delegates to the **real** handler (never a copy of its formatting — a copy
drifts), and one :func:`register_screen` call below.

The handler is imported lazily inside the renderer: ``app.handlers.menu`` pulls
in aiogram and half the service layer, and the cabinet must not pay for that on
import.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from app.localization.texts import Texts


# (texts, db) -> rendered message text
ScreenRenderer = Callable[['Texts', Any], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class ScreenDefinition:
    """One previewable screen."""

    id: str
    title: str
    description: str
    render: ScreenRenderer


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


async def _render_main_menu(texts: Texts, db: Any) -> str:
    """The main menu, rendered by the bot's own ``get_main_menu_text``.

    In multi-tariff mode the handler reads the subscription list straight from
    the DB by user id; the synthetic user has none, so the preview shows the
    "no subscriptions" variant of the block. Single-tariff mode (the default)
    reads ``user.subscription`` off the synthetic instance and shows the full
    status line.
    """
    from app.handlers.menu import get_main_menu_text

    from .synthetic import build_synthetic_user

    user = build_synthetic_user(texts.language)
    return await get_main_menu_text(user, texts, db)


register_screen(
    ScreenDefinition(
        id='main_menu',
        title='Главное меню',
        description='Первый экран после /start: имя пользователя, статус подписки и приглашение выбрать действие.',
        render=_render_main_menu,
    )
)
