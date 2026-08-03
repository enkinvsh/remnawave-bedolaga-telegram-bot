"""Sample user for screen previews — built in memory, never persisted.

The preview must render the same code path the bot runs, and that code path
wants a ``User`` with a ``Subscription``. We hand it detached (transient) ORM
instances: they are **never** added to a session, so nothing can be flushed to
the database, and they carry obviously fake data so a preview can never be
mistaken for a real customer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.database.models import Subscription, SubscriptionStatus, User, UserStatus


# Обозримо фальшивые реквизиты: 0 не может быть настоящим telegram_id.
SYNTHETIC_TELEGRAM_ID = 0
SYNTHETIC_USER_ID = 0


def build_synthetic_user(language: str = 'ru') -> User:
    """A transient ``User`` with one active subscription.

    Not added to any session — callers must keep it that way.

    ``Subscription.id`` deliberately stays ``None``: ``build_test_access_hint``
    bails out on a falsy id, so rendering the main menu never issues a query for
    a row that does not exist.
    """
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

    subscription = Subscription(
        user_id=SYNTHETIC_USER_ID,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now - timedelta(days=10),
        end_date=now + timedelta(days=30),
        traffic_limit_gb=100,
        traffic_used_gb=12.5,
        purchased_traffic_gb=0,
        device_limit=3,
        connected_squads=[],
        autopay_enabled=False,
        created_at=now - timedelta(days=10),
        updated_at=now,
    )

    user.subscriptions = [subscription]
    return user
