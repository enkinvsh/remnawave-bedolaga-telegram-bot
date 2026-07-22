from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LifecycleMessageLog, Subscription, Transaction, TransactionType
from app.services.winback_oneoff_audience import Cohort, TargetChannel, event_key_for, select_audience


WINBACK_SENT_TODAY_NOTE: Final = 'Прокси: все winback-маркеры за сегодня; канал доставки в журнале не хранится.'


@dataclass(frozen=True, slots=True)
class FunnelItem:
    rule_key: str
    touched: int
    trials_after: int
    deposits_after: int
    revenue_after_kopeks: int


@dataclass(frozen=True, slots=True)
class TimelineDay:
    date: str
    by_event: dict[str, int]


@dataclass(frozen=True, slots=True)
class WinbackCohort:
    cohort: Cohort
    touched: int
    remaining_email: int
    remaining_telegram: int


@dataclass(frozen=True, slots=True)
class WinbackStats:
    cohorts: list[WinbackCohort]
    email_sent_today: int


async def get_funnel(db: AsyncSession) -> list[FunnelItem]:
    first_markers = (
        select(
            LifecycleMessageLog.user_id.label('user_id'),
            LifecycleMessageLog.rule_key.label('rule_key'),
            func.min(LifecycleMessageLog.sent_at).label('first_sent_at'),
        )
        .group_by(LifecycleMessageLog.user_id, LifecycleMessageLog.rule_key)
        .cte('first_markers')
    )
    touched = (
        select(first_markers.c.rule_key, func.count().label('touched'))
        .group_by(first_markers.c.rule_key)
        .cte('touched')
    )
    trials = (
        select(first_markers.c.rule_key, func.count(func.distinct(first_markers.c.user_id)).label('trials_after'))
        .join(
            Subscription,
            and_(
                Subscription.user_id == first_markers.c.user_id,
                Subscription.is_trial.is_(True),
                Subscription.created_at > first_markers.c.first_sent_at,
            ),
        )
        .group_by(first_markers.c.rule_key)
        .cte('trials')
    )
    deposits = (
        select(
            first_markers.c.rule_key,
            func.count(func.distinct(first_markers.c.user_id)).label('deposits_after'),
            func.sum(Transaction.amount_kopeks).label('revenue_after_kopeks'),
        )
        .join(
            Transaction,
            and_(
                Transaction.user_id == first_markers.c.user_id,
                Transaction.type == TransactionType.DEPOSIT.value,
                Transaction.is_completed.is_(True),
                Transaction.created_at > first_markers.c.first_sent_at,
            ),
        )
        .group_by(first_markers.c.rule_key)
        .cte('deposits')
    )
    result = await db.execute(
        select(
            touched.c.rule_key,
            touched.c.touched,
            func.coalesce(trials.c.trials_after, 0),
            func.coalesce(deposits.c.deposits_after, 0),
            func.coalesce(deposits.c.revenue_after_kopeks, 0),
        )
        .outerjoin(trials, trials.c.rule_key == touched.c.rule_key)
        .outerjoin(deposits, deposits.c.rule_key == touched.c.rule_key)
        .order_by(touched.c.rule_key)
    )
    return [FunnelItem(*row) for row in result.all()]


async def get_timeline(db: AsyncSession, days: int, now: datetime | None = None) -> list[TimelineDay]:
    current_time = now or datetime.now(UTC)
    first_date = current_time.date() - timedelta(days=days - 1)
    first_datetime = datetime.combine(first_date, datetime.min.time(), tzinfo=UTC)
    tomorrow_datetime = datetime.combine(current_time.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    result = await db.execute(
        select(
            func.date(LifecycleMessageLog.sent_at).label('sent_date'),
            LifecycleMessageLog.rule_key,
            func.count(LifecycleMessageLog.id),
        )
        .where(LifecycleMessageLog.sent_at >= first_datetime, LifecycleMessageLog.sent_at < tomorrow_datetime)
        .group_by('sent_date', LifecycleMessageLog.rule_key)
        .order_by('sent_date', LifecycleMessageLog.rule_key)
    )
    counts: dict[str, dict[str, int]] = {}
    for sent_date, rule_key, count in result.all():
        key = sent_date.isoformat() if isinstance(sent_date, date) else str(sent_date)[:10]
        counts.setdefault(key, {})[rule_key] = count
    return [
        TimelineDay(date=(first_date + timedelta(days=offset)).isoformat(), by_event=counts.get((first_date + timedelta(days=offset)).isoformat(), {}))
        for offset in range(days)
    ]


async def _count_today_winback_markers(db: AsyncSession, now: datetime) -> int:
    today_start = datetime.combine(now.date(), datetime.min.time(), tzinfo=UTC)
    marker_keys = tuple(event_key_for(cohort) for cohort in Cohort)
    result = await db.execute(
        select(func.count(LifecycleMessageLog.id)).where(
            LifecycleMessageLog.rule_key.in_(marker_keys),
            LifecycleMessageLog.occurrence == 1,
            LifecycleMessageLog.sent_at >= today_start,
            LifecycleMessageLog.sent_at < today_start + timedelta(days=1),
        )
    )
    return result.scalar_one() or 0


async def get_winback_stats(db: AsyncSession, now: datetime | None = None) -> WinbackStats:
    current_time = now or datetime.now(UTC)
    cohorts: list[WinbackCohort] = []
    for cohort in Cohort:
        audience = await select_audience(db, current_time, cohort)
        cohorts.append(
            WinbackCohort(
                cohort=cohort,
                touched=audience.already_sent,
                remaining_email=sum(target.channel is TargetChannel.EMAIL for target in audience.targets),
                remaining_telegram=sum(target.channel is TargetChannel.TELEGRAM for target in audience.targets),
            )
        )
    # Lifecycle markers do not record delivery channel, so this is the honest available proxy.
    return WinbackStats(cohorts=cohorts, email_sent_today=await _count_today_winback_markers(db, current_time))
