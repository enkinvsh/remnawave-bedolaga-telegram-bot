"""Lifecycle-триггеры: выборка аудитории и оркестрация (задача C2).

Каждое правило — чистая функция выборки кандидатов (``select_*``), которые оркестрирует
``run_lifecycle_triggers`` (построение/отправка сообщений — в ``lifecycle_trigger_messages``).
Тайминги/тексты/кнопки НЕ хардкодятся: читаются из реестра ``services.lifecycle_rules`` с
БД-оверрайдом (``crud.lifecycle``). Дедуп по-шагово через ``was_sent``/``record_sent``.

Разграничение аудиторий (чтобы клиент НИКОГДА не получил две лестницы):
    * ``post_trial_ladder`` бьёт только по ИСТЁКШИМ ТРИАЛАМ пользователей, которые
      НИКОГДА не платили (``User.has_had_paid_subscription is False``) и не имеют
      активной подписки.
    * Легаси ``expired_1d``/``expired_second_wave``/``expired_third_wave`` в
      ``monitoring_service._check_expired_subscription_followups`` фильтруют
      ``Subscription.is_trial == False`` — то есть только ПЛАТНЫЕ подписки.
    Множества не пересекаются: подписка либо триальная, либо платная, а «платившие»
    вообще исключены из ``post_trial_ladder``.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from app.config import settings
from app.database.crud.lifecycle import get_all_rules, record_sent, was_sent
from app.database.models import Subscription, SubscriptionStatus, User, UserStatus
from app.services.lifecycle_rules import default_enabled, merged_config
from app.services.lifecycle_trigger_messages import (
    LifecycleCandidate,
    handle_post_trial_ladder,
    handle_trial_ending,
    handle_trial_not_activated,
    handle_trial_zero_traffic,
)
from app.services.notification_settings_service import NotificationSettingsService


logger = structlog.get_logger(__name__)


def compute_repeat_occurrence(
    anchor: datetime,
    now: datetime,
    *,
    first_offset_hours: int,
    repeat_hours: int,
    max_repeats: int,
) -> int | None:
    """occ=1 в ``+first_offset``; далее +1 каждые ``repeat_hours``; потолок ``1+max_repeats``."""
    elapsed_hours = (now - anchor).total_seconds() / 3600.0
    if elapsed_hours < first_offset_hours:
        return None
    if repeat_hours <= 0:
        return 1
    repeats_done = int((elapsed_hours - first_offset_hours) // repeat_hours)
    return 1 + min(repeats_done, max(0, max_repeats))


def compute_offset_occurrence(
    anchor: datetime,
    now: datetime,
    offsets_hours: list[int],
) -> tuple[int, int] | None:
    """Последний наступивший из ``offsets_hours`` → (1-based occurrence, сам offset)."""
    elapsed_hours = (now - anchor).total_seconds() / 3600.0
    matched: tuple[int, int] | None = None
    for index, offset in enumerate(offsets_hours, start=1):
        if elapsed_hours >= offset:
            matched = (index, offset)
    return matched


def _step_offset_hours(step: dict[str, Any]) -> float:
    raw_hours = step.get('offset_hours')
    if raw_hours is not None:
        return float(raw_hours)
    return float(step.get('offset_days', 0)) * 24.0


def compute_ladder_step(
    anchor: datetime,
    now: datetime,
    steps: list[dict[str, Any]],
) -> tuple[int, dict[str, Any]] | None:
    """Последняя наступившая ступень лестницы → (1-based номер, конфиг ступени)."""
    elapsed_hours = (now - anchor).total_seconds() / 3600.0
    matched: tuple[int, dict[str, Any]] | None = None
    for index, step in enumerate(steps, start=1):
        if elapsed_hours >= _step_offset_hours(step):
            matched = (index, step)
    return matched


def _reachable_user_filters() -> list[Any]:
    return [User.telegram_id.isnot(None), User.status == UserStatus.ACTIVE.value]


def _active_trial_filters(now: datetime) -> list[Any]:
    return [
        Subscription.is_trial.is_(True),
        Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
        Subscription.end_date > now,
    ]


async def select_trial_not_activated(
    db: AsyncSession, config: dict[str, Any], now: datetime, limit: int
) -> list[LifecycleCandidate]:
    """Зарегистрированы, триал доступен, подписки НИКОГДА не было."""
    first = int(config.get('first_offset_hours', 1))
    repeat = int(config.get('repeat_hours', 48))
    max_repeats = int(config.get('max_repeats', 3))
    window_hours = first + (max_repeats + 1) * max(repeat, 1)

    stmt = (
        select(User)
        .where(
            *_reachable_user_filters(),
            User.has_had_paid_subscription.is_(False),
            User.created_at <= now - timedelta(hours=first),
            User.created_at >= now - timedelta(hours=window_hours),
            ~exists().where(Subscription.user_id == User.id),
        )
        .order_by(User.created_at.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)

    candidates: list[LifecycleCandidate] = []
    for user in result.scalars().all():
        if user.created_at is None:
            continue
        occurrence = compute_repeat_occurrence(
            user.created_at, now, first_offset_hours=first, repeat_hours=repeat, max_repeats=max_repeats
        )
        if occurrence is not None:
            candidates.append(LifecycleCandidate(user=user, occurrence=occurrence))
    return candidates


async def select_trial_zero_traffic(
    db: AsyncSession, config: dict[str, Any], now: datetime, limit: int
) -> list[LifecycleCandidate]:
    """Активный триал с нулевым трафиком (подписка И суммарный по юзеру = 0)."""
    offsets = sorted(int(hours) for hours in config.get('offsets_hours', [1, 24]))
    if not offsets:
        return []

    stmt = (
        select(Subscription)
        .join(User, Subscription.user_id == User.id)
        .options(selectinload(Subscription.user))
        .where(
            *_reachable_user_filters(),
            *_active_trial_filters(now),
            User.lifetime_used_traffic_bytes == 0,
            Subscription.traffic_used_gb == 0,
            Subscription.start_date <= now - timedelta(hours=offsets[0]),
            Subscription.start_date >= now - timedelta(hours=offsets[-1] + 24),
        )
        .order_by(Subscription.start_date.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)

    candidates: list[LifecycleCandidate] = []
    for subscription in result.scalars().all():
        if subscription.user is None or subscription.start_date is None:
            continue
        match = compute_offset_occurrence(subscription.start_date, now, offsets)
        if match is not None:
            occurrence, offset = match
            candidates.append(
                LifecycleCandidate(
                    user=subscription.user, occurrence=occurrence, subscription=subscription, offset_hours=offset
                )
            )
    return candidates


async def select_trial_ending(
    db: AsyncSession, config: dict[str, Any], now: datetime, limit: int
) -> list[LifecycleCandidate]:
    """Активный триал, до конца которого осталось не больше ``hours_before``."""
    hours_before = int(config.get('hours_before', 2))
    stmt = (
        select(Subscription)
        .join(User, Subscription.user_id == User.id)
        .options(selectinload(Subscription.user))
        .where(
            *_reachable_user_filters(),
            *_active_trial_filters(now),
            Subscription.end_date <= now + timedelta(hours=hours_before),
        )
        .order_by(Subscription.end_date.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return [
        LifecycleCandidate(user=subscription.user, occurrence=1, subscription=subscription)
        for subscription in result.scalars().all()
        if subscription.user is not None
    ]


async def select_post_trial_ladder(
    db: AsyncSession, config: dict[str, Any], now: datetime, limit: int
) -> list[LifecycleCandidate]:
    """Истёкший триал, юзер НИКОГДА не платил и не имеет активной подписки.

    ``has_had_paid_subscription is False`` + ``is_trial is True`` держит эту лестницу
    непересекающейся с платными expired-волнами (см. модульный docstring).
    """
    steps = config.get('steps', [])
    if not steps:
        return []

    offsets = [_step_offset_hours(step) for step in steps]
    other_active = aliased(Subscription)
    stmt = (
        select(Subscription)
        .join(User, Subscription.user_id == User.id)
        .options(selectinload(Subscription.user), selectinload(Subscription.tariff))
        .where(
            *_reachable_user_filters(),
            User.has_had_paid_subscription.is_(False),
            Subscription.is_trial.is_(True),
            Subscription.status == SubscriptionStatus.EXPIRED.value,
            Subscription.end_date <= now - timedelta(hours=min(offsets)),
            Subscription.end_date >= now - timedelta(hours=max(offsets) + 48),
            ~exists().where(
                and_(
                    other_active.user_id == User.id,
                    other_active.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
                    other_active.end_date > now,
                )
            ),
        )
        .order_by(Subscription.end_date.asc())
        .limit(limit)
    )
    result = await db.execute(stmt)

    candidates: list[LifecycleCandidate] = []
    for subscription in result.scalars().all():
        if subscription.user is None or subscription.end_date is None:
            continue
        match = compute_ladder_step(subscription.end_date, now, steps)
        if match is not None:
            occurrence, step = match
            candidates.append(
                LifecycleCandidate(user=subscription.user, occurrence=occurrence, subscription=subscription, step=step)
            )
    return candidates


_RULES: dict[str, tuple[Any, Any]] = {
    'trial_not_activated': (select_trial_not_activated, handle_trial_not_activated),
    'trial_zero_traffic': (select_trial_zero_traffic, handle_trial_zero_traffic),
    'trial_ending': (select_trial_ending, handle_trial_ending),
    'post_trial_ladder': (select_post_trial_ladder, handle_post_trial_ladder),
}


async def run_lifecycle_triggers(
    db: AsyncSession,
    send_message: Any,
    now: datetime | None = None,
) -> dict[str, int]:
    """Один прогон всех lifecycle-правил. Возвращает ``{rule_key: отправлено}``.

    Дешёвый при выключенных правилах: выборка кандидатов НЕ выполняется для
    отключённого правила. Изоляция ошибок — по каждому пользователю отдельно.
    """
    if not NotificationSettingsService.are_notifications_globally_enabled():
        return {}

    now = now or datetime.now(UTC)
    overrides = {rule.key: rule for rule in await get_all_rules(db)}
    batch_limit = max(1, int(getattr(settings, 'LIFECYCLE_TRIGGERS_BATCH_LIMIT', 100)))
    sent_totals: dict[str, int] = {}

    for key, (select_candidates, handle_candidate) in _RULES.items():
        override = overrides.get(key)
        enabled = override.enabled if override is not None else default_enabled(key)
        if not enabled:
            continue

        config = merged_config(key, override.config if override is not None else None)
        try:
            candidates = await select_candidates(db, config, now, batch_limit)
        except Exception as exc:
            logger.error('lifecycle: ошибка выборки кандидатов', rule_key=key, exc=exc)
            continue

        sent = 0
        for candidate in candidates:
            if sent >= batch_limit:
                break
            try:
                if await was_sent(db, candidate.user.id, key, candidate.occurrence):
                    continue
                if await handle_candidate(db, send_message, candidate, config, now):
                    await record_sent(db, candidate.user.id, key, candidate.occurrence)
                    sent += 1
            except Exception as exc:
                logger.error(
                    'lifecycle: ошибка отправки правила пользователю',
                    rule_key=key,
                    user_id=candidate.user.id,
                    exc=exc,
                )
                continue

        if sent:
            sent_totals[key] = sent
            logger.info('lifecycle: отправлены сообщения правила', rule_key=key, count=sent)

    return sent_totals
