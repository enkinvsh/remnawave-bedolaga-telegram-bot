from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.sql.elements import ColumnElement

from app.config import settings
from app.database.models import Subscription, SubscriptionStatus, Tariff, User, UserStatus
from app.services import lifecycle_rules
from app.services.lifecycle_trigger_service import compute_ladder_step
from app.services.notification_settings_service import NotificationSettingsService


class EmailLifecycleUser(Protocol):
    id: int
    email: str
    promo_emails_opt_out_at: datetime | None
    promo_offer_discount_percent: int
    promo_offer_discount_source: str | None
    promo_offer_discount_expires_at: datetime | None
    updated_at: datetime


class EmailLifecycleSubscription(Protocol):
    id: int
    end_date: datetime
    tariff: 'EmailLifecycleTariff | None'


class EmailLifecycleTariff(Protocol):
    is_daily: bool


@dataclass(frozen=True, slots=True)
class EmailLifecycleCandidate:
    user: EmailLifecycleUser
    subscription: EmailLifecycleSubscription
    occurrence: int
    step: dict[str, Any] | None = None


def _email_only_v1_filters() -> list[ColumnElement[bool]]:
    users = User.__table__.c
    return [
        users.email.isnot(None),
        users.email_verified.is_(True),
        users.telegram_id.is_(None),
        users.status == UserStatus.ACTIVE.value,
    ]


async def select_trial_ending_email(
    db: AsyncSession, config: dict[str, Any], now: datetime, limit: int, offset: int = 0
) -> list[EmailLifecycleCandidate]:
    hours_before = int(config.get('hours_before', 2))
    users = User.__table__.c
    subscriptions = Subscription.__table__.c
    result = await db.execute(
        select(Subscription)
        .join(User, subscriptions.user_id == users.id)
        .options(selectinload(Subscription.user))
        .where(
            *_email_only_v1_filters(),
            subscriptions.is_trial.is_(True),
            subscriptions.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
            subscriptions.end_date > now,
            subscriptions.end_date <= now + timedelta(hours=hours_before),
        )
        .order_by(subscriptions.end_date.asc(), subscriptions.id.asc())
        .offset(offset)
        .limit(limit)
    )
    return [
        EmailLifecycleCandidate(user=subscription.user, subscription=subscription, occurrence=1)
        for subscription in result.scalars().all()
        if subscription.user is not None
    ]


async def select_post_trial_email(
    db: AsyncSession, config: dict[str, Any], now: datetime, limit: int, offset: int = 0
) -> list[EmailLifecycleCandidate]:
    steps = [step for step in config.get('steps', []) if int(step.get('discount_percent', 0)) > 0]
    if not steps:
        return []
    offsets = [
        float(step['offset_hours']) if step.get('offset_hours') is not None else float(step.get('offset_days', 0)) * 24
        for step in steps
    ]
    users = User.__table__.c
    subscriptions = Subscription.__table__.c
    other_active = Subscription.__table__.alias('other_active').c
    result = await db.execute(
        select(Subscription)
        .join(User, subscriptions.user_id == users.id)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .where(
            *_email_only_v1_filters(),
            users.promo_emails_opt_out_at.is_(None),
            users.has_had_paid_subscription.is_(False),
            subscriptions.is_trial.is_(True),
            subscriptions.status == SubscriptionStatus.EXPIRED.value,
            subscriptions.end_date <= now - timedelta(hours=min(offsets)),
            subscriptions.end_date >= now - timedelta(hours=max(offsets) + 48),
            ~exists().where(
                and_(
                    other_active.user_id == users.id,
                    other_active.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
                    other_active.end_date > now,
                )
            ),
        )
        .order_by(subscriptions.end_date.asc(), subscriptions.id.asc())
        .offset(offset)
        .limit(limit)
    )
    candidates: list[EmailLifecycleCandidate] = []
    for subscription in result.scalars().all():
        if subscription.user is None or subscription.end_date is None:
            continue
        matched = compute_ladder_step(subscription.__dict__['end_date'], now, steps)
        if matched is not None:
            occurrence, step = matched
            candidates.append(
                EmailLifecycleCandidate(
                    user=subscription.user,
                    subscription=subscription,
                    occurrence=occurrence,
                    step=step,
                )
            )
    return candidates


async def _select_paid_winback_email(
    db: AsyncSession,
    now: datetime,
    limit: int,
    offset: int,
    *,
    third_wave: bool,
) -> list[EmailLifecycleCandidate]:
    users = User.__table__.c
    subscriptions = Subscription.__table__.c
    tariffs = Tariff.__table__.c
    other_active = Subscription.__table__.alias('other_active').c
    trigger_days = (
        NotificationSettingsService.get_third_wave_trigger_days()
        if third_wave
        else lifecycle_rules.EXPIRED_SECOND_WAVE_TRIGGER_DAYS
    )
    window_days = (
        lifecycle_rules.EXPIRED_THIRD_WAVE_WINDOW_DAYS
        if third_wave
        else lifecycle_rules.EXPIRED_SECOND_WAVE_WINDOW_DAYS
    )
    stmt = (
        select(Subscription)
        .join(User, subscriptions.user_id == users.id)
        .outerjoin(Tariff, subscriptions.tariff_id == tariffs.id)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .where(
            *_email_only_v1_filters(),
            users.promo_emails_opt_out_at.is_(None),
            subscriptions.is_trial.is_(False),
            subscriptions.status == SubscriptionStatus.EXPIRED.value,
            subscriptions.end_date <= now - timedelta(days=trigger_days),
            subscriptions.end_date > now - timedelta(days=trigger_days + window_days),
            or_(subscriptions.tariff_id.is_(None), tariffs.is_daily.is_(False)),
        )
        .order_by(subscriptions.end_date.asc(), subscriptions.id.asc())
        .offset(offset)
        .limit(limit)
    )
    if settings.is_multi_tariff_enabled():
        stmt = stmt.where(
            ~exists().where(
                and_(
                    other_active.user_id == users.id,
                    other_active.id != subscriptions.id,
                    other_active.status == SubscriptionStatus.ACTIVE.value,
                    other_active.end_date > now,
                )
            )
        )
    result = await db.execute(stmt)
    candidates: list[EmailLifecycleCandidate] = []
    for subscription in result.scalars().all():
        if subscription.user is None or subscription.end_date is None:
            continue
        candidates.append(EmailLifecycleCandidate(subscription.user, subscription, 1))
    return candidates


async def select_second_winback_email(
    db: AsyncSession, _config: dict[str, Any], now: datetime, limit: int, offset: int = 0
) -> list[EmailLifecycleCandidate]:
    return await _select_paid_winback_email(db, now, limit, offset, third_wave=False)


async def select_third_winback_email(
    db: AsyncSession, _config: dict[str, Any], now: datetime, limit: int, offset: int = 0
) -> list[EmailLifecycleCandidate]:
    return await _select_paid_winback_email(db, now, limit, offset, third_wave=True)
