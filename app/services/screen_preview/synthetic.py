"""Sample user for screen previews — built in memory, never persisted.

The preview must render the same code path the bot runs, and that code path
wants a ``User`` with a ``Subscription``. We hand it detached (transient) ORM
instances: they are **never** added to a session, so nothing can be flushed to
the database, and they carry obviously fake data so a preview can never be
mistaken for a real customer.

One user is not enough. The main menu picks a different string for every
subscription state, so a preview stuck on "active, 30 days left" hides
«Истекла», «Отключена», «Лимит трафика» and the rest — the owner would be
editing them blind. :data:`SYNTHETIC_STATES` therefore lists one state per
branch of ``_get_subscription_status``; each is still pure in-memory data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from app.database.models import Subscription, SubscriptionStatus, User, UserStatus


logger = structlog.get_logger(__name__)


# Обозримо фальшивые реквизиты: 0 не может быть настоящим telegram_id.
SYNTHETIC_TELEGRAM_ID = 0
SYNTHETIC_USER_ID = 0


@dataclass(frozen=True, slots=True)
class SyntheticState:
    """One previewable subscription state."""

    id: str
    label: str


# Порядок = порядок в выпадающем списке кабинета: сначала «всё хорошо»,
# потом проблемные состояния, в конце «подписки нет».
SYNTHETIC_STATES: tuple[SyntheticState, ...] = (
    SyntheticState('active_long', 'Активна, больше недели'),
    SyntheticState('active_few_days', 'Активна, несколько дней'),
    SyntheticState('active_tomorrow', 'Активна, истекает завтра'),
    SyntheticState('active_today', 'Активна, истекает сегодня'),
    SyntheticState('expired', 'Истекла'),
    SyntheticState('disabled', 'Отключена'),
    SyntheticState('limited', 'Лимит трафика'),
    SyntheticState('trial', 'Тестовая подписка'),
    SyntheticState('pending', 'Ожидает оплаты'),
    SyntheticState('none', 'Без подписки'),
)

DEFAULT_SYNTHETIC_STATE = 'active_long'

SYNTHETIC_STATE_IDS: frozenset[str] = frozenset(state.id for state in SYNTHETIC_STATES)

# state -> (статус подписки, is_trial, срок относительно "сейчас").
# Часы в дельтах не косметика: days_left считается как (end - now).days, поэтому
# ровно +1 день округлился бы до 0 и увёл бы рендер не в ту ветку.
_BLUEPRINTS: dict[str, tuple[str, bool, timedelta]] = {
    'active_long': (SubscriptionStatus.ACTIVE.value, False, timedelta(days=30, hours=1)),
    'active_few_days': (SubscriptionStatus.ACTIVE.value, False, timedelta(days=3, hours=1)),
    'active_tomorrow': (SubscriptionStatus.ACTIVE.value, False, timedelta(days=1, hours=1)),
    'active_today': (SubscriptionStatus.ACTIVE.value, False, timedelta(hours=5)),
    'expired': (SubscriptionStatus.EXPIRED.value, False, timedelta(days=-3)),
    'disabled': (SubscriptionStatus.DISABLED.value, False, timedelta(days=10)),
    'limited': (SubscriptionStatus.LIMITED.value, False, timedelta(days=10)),
    'trial': (SubscriptionStatus.TRIAL.value, True, timedelta(days=5, hours=1)),
    'pending': (SubscriptionStatus.PENDING.value, False, timedelta(days=10)),
    # 'none' — подписки нет вовсе, поэтому в таблице его нет.
}


def is_known_state(state: str) -> bool:
    return state in SYNTHETIC_STATE_IDS


def build_synthetic_user(language: str = 'ru', state: str = DEFAULT_SYNTHETIC_STATE) -> User:
    """A transient ``User`` in the requested subscription ``state``.

    Not added to any session — callers must keep it that way.

    ``Subscription.id`` deliberately stays ``None``: ``build_test_access_hint``
    bails out on a falsy id, so rendering the main menu never issues a query for
    a row that does not exist.

    Raises ``ValueError`` for an unknown state.
    """
    if not is_known_state(state):
        raise ValueError(f'Unknown synthetic state: {state}')

    now = datetime.now(UTC)

    user = User(
        id=SYNTHETIC_USER_ID,
        telegram_id=SYNTHETIC_TELEGRAM_ID,
        auth_type='telegram',
        username='demo_preview',
        first_name='Демо',
        last_name='Пользователь',
        status=UserStatus.ACTIVE.value,
        language=language,
        balance_kopeks=125_000,
        used_promocodes=0,
        has_had_paid_subscription=True,
        referral_code='DEMO0000',
        created_at=now - timedelta(days=90),
        updated_at=now,
        last_activity=now,
    )

    blueprint = _BLUEPRINTS.get(state)
    user.subscriptions = [] if blueprint is None else [_build_subscription(now, blueprint)]
    return user


async def attach_sample_tariff(db: Any, user: User) -> bool:
    """Point the synthetic subscription at a REAL tariff row. Read-only.

    ``get_main_menu_text`` renders the tariff line only when
    ``get_tariff_by_id`` returns a row, so a synthetic subscription with no
    ``tariff_id`` produced a preview missing a line every real user sees — and
    ``MAIN_MENU_TARIFF_LINE`` was uneditable in practice because the owner
    could not see the result of editing it.

    We borrow the id of an existing active tariff instead of faking one:
    nothing is written, and the preview shows the same shape the bot renders.
    Returns ``True`` when a tariff was attached.
    """
    if db is None or not user.subscriptions:
        return False

    try:
        from app.database.crud.tariff import get_all_active_tariffs

        tariffs = await get_all_active_tariffs(db)
    except Exception as error:  # превью не должно падать из-за тарифов
        logger.debug('Не удалось подобрать тариф для превью', error=error)
        return False

    if not tariffs:
        return False

    user.subscriptions[0].tariff_id = tariffs[0].id
    return True


def _build_subscription(now: datetime, blueprint: tuple[str, bool, timedelta]) -> Subscription:
    status, is_trial, remaining = blueprint

    return Subscription(
        user_id=SYNTHETIC_USER_ID,
        status=status,
        is_trial=is_trial,
        start_date=now - timedelta(days=10),
        end_date=now + remaining,
        traffic_limit_gb=100,
        traffic_used_gb=100.0 if status == SubscriptionStatus.LIMITED.value else 12.5,
        purchased_traffic_gb=0,
        device_limit=3,
        connected_squads=[],
        autopay_enabled=False,
        created_at=now - timedelta(days=10),
        updated_at=now,
    )
