"""Audience query and contact-channel partitioning for one-off win-back."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol, assert_never

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    LifecycleMessageLog,
    Subscription,
    SubscriptionStatus,
    Transaction,
    TransactionType,
    User,
    UserStatus,
)


SILENCE_DAYS: Final = 28


class Channel(StrEnum):
    EMAIL = 'email'
    TELEGRAM = 'telegram'
    BOTH = 'both'


class TargetChannel(StrEnum):
    EMAIL = 'email'
    TELEGRAM = 'telegram'


class Cohort(StrEnum):
    A = 'a'
    B = 'b'
    C = 'c'
    D = 'd'


EVENT_KEY: Final = 'winback_oneoff'


def event_key_for(cohort: Cohort) -> str:
    match cohort:
        case Cohort.A:
            return EVENT_KEY
        case Cohort.B:
            return 'winback_trial_b'
        case Cohort.C:
            return 'winback_trial_c'
        case Cohort.D:
            return 'winback_trial_d'
        case unreachable:
            assert_never(unreachable)


class WinbackUser(Protocol):
    id: int
    email: str | None
    email_verified: bool
    promo_emails_opt_out_at: datetime | None
    telegram_id: int | None
    promo_offer_discount_percent: int
    promo_offer_discount_source: str | None
    promo_offer_discount_expires_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EmailTarget:
    user: WinbackUser
    subscription_id: int | None
    email: str

    @property
    def channel(self) -> TargetChannel:
        return TargetChannel.EMAIL


@dataclass(frozen=True, slots=True)
class TelegramTarget:
    user: WinbackUser
    subscription_id: int | None
    telegram_id: int

    @property
    def channel(self) -> TargetChannel:
        return TargetChannel.TELEGRAM


type Target = EmailTarget | TelegramTarget


@dataclass(frozen=True, slots=True)
class CohortTarget:
    cohort: Cohort
    target: Target


@dataclass(frozen=True, slots=True)
class Audience:
    targets: tuple[Target, ...]
    already_sent: int
    cohort: Cohort = Cohort.A


def _target_for(user: WinbackUser, subscription_id: int | None) -> Target | None:
    if user.email is not None and user.email_verified and user.promo_emails_opt_out_at is None:
        return EmailTarget(user, subscription_id, user.email)
    if user.telegram_id is not None:
        return TelegramTarget(user, subscription_id, user.telegram_id)
    return None


async def select_audience(db: AsyncSession, now: datetime, cohort: Cohort = Cohort.A) -> Audience:
    cutoff = now - timedelta(days=SILENCE_DAYS)
    deposits = Transaction.__table__.alias('deposits')
    recent_deposits = Transaction.__table__.alias('recent_deposits')
    active_subscriptions = Subscription.__table__.alias('active_subscriptions')
    trial_subscriptions = Subscription.__table__.alias('trial_subscriptions')
    logs = LifecycleMessageLog.__table__.alias('winback_logs')
    subscriptions = Subscription.__table__.c
    latest_subscription_id = (
        select(subscriptions.id)
        .where(subscriptions.user_id == User.id)
        .order_by(subscriptions.end_date.desc(), subscriptions.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    any_deposit = exists().where(
        deposits.c.user_id == User.id,
        deposits.c.type == TransactionType.DEPOSIT.value,
        deposits.c.is_completed.is_(True),
    )
    any_trial = exists().where(
        trial_subscriptions.c.user_id == User.id,
        trial_subscriptions.c.is_trial.is_(True),
    )
    latest_trial_end = (
        select(func.max(trial_subscriptions.c.end_date))
        .where(
            trial_subscriptions.c.user_id == User.id,
            trial_subscriptions.c.is_trial.is_(True),
        )
        .correlate(User)
        .scalar_subquery()
    )
    maximum_trial_traffic = (
        select(func.max(trial_subscriptions.c.traffic_used_gb))
        .where(
            trial_subscriptions.c.user_id == User.id,
            trial_subscriptions.c.is_trial.is_(True),
        )
        .correlate(User)
        .scalar_subquery()
    )
    match cohort:
        case Cohort.A:
            cohort_filters = (
                any_deposit,
                ~exists().where(
                    recent_deposits.c.user_id == User.id,
                    recent_deposits.c.type == TransactionType.DEPOSIT.value,
                    recent_deposits.c.is_completed.is_(True),
                    recent_deposits.c.created_at >= cutoff,
                ),
            )
        case Cohort.B:
            cohort_filters = (~any_deposit, ~any_trial)
        case Cohort.C:
            cohort_filters = (
                ~any_deposit,
                any_trial,
                latest_trial_end < now,
                func.coalesce(maximum_trial_traffic, 0) == 0,
            )
        case Cohort.D:
            cohort_filters = (
                ~any_deposit,
                any_trial,
                latest_trial_end < now,
                func.coalesce(maximum_trial_traffic, 0) > 0,
            )
        case unreachable:
            assert_never(unreachable)
    was_sent = exists().where(
        logs.c.user_id == User.id,
        logs.c.rule_key.like('winback%'),
        logs.c.occurrence == 1,
    )
    statement = (
        select(User, latest_subscription_id.label('subscription_id'), was_sent.label('already_sent'))
        .where(
            User.status == UserStatus.ACTIVE.value,
            *cohort_filters,
            ~exists().where(
                active_subscriptions.c.user_id == User.id,
                active_subscriptions.c.status.in_(
                    [SubscriptionStatus.ACTIVE.value, SubscriptionStatus.LIMITED.value]
                ),
                active_subscriptions.c.is_trial.is_(False),
                active_subscriptions.c.end_date > now,
            ),
        )
        .order_by(User.id.asc())
    )
    targets: list[Target] = []
    already_sent = 0
    for user, subscription_id, sent in (await db.execute(statement)).all():
        target = _target_for(user, subscription_id)
        if target is None:
            continue
        if sent:
            already_sent += 1
            continue
        targets.append(target)
    return Audience(tuple(targets), already_sent, cohort)
