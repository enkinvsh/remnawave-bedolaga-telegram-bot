"""CRUD для lifecycle-правил и журнала отправок.

``lifecycle_rules`` — конфиг-оверрайды поверх реестра ``services/lifecycle_rules``.
``lifecycle_message_log`` — дедуп-журнал (одна строка = один отправленный шаг).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import structlog
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LifecycleMessageLog, LifecycleRule, User


logger = structlog.get_logger(__name__)
_EMAIL_RECENT_LIMIT: Final = 20


@dataclass(frozen=True, slots=True)
class EmailLifecycleRecentDelivery:
    event_key: str
    user_id: int
    email: str
    sent_at: datetime


@dataclass(frozen=True, slots=True)
class EmailLifecycleStats:
    optout_count: int
    audience_count: int
    totals_30d: dict[str, int]
    recent: list[EmailLifecycleRecentDelivery]


async def get_all_rules(db: AsyncSession) -> list[LifecycleRule]:
    """Все строки-оверрайды правил из БД (без учёта дефолтов реестра)."""
    result = await db.execute(select(LifecycleRule))
    return list(result.scalars().all())


async def get_rule(db: AsyncSession, key: str) -> LifecycleRule | None:
    """Строка-оверрайд конкретного правила или ``None``, если её нет в БД."""
    result = await db.execute(select(LifecycleRule).where(LifecycleRule.key == key))
    return result.scalar_one_or_none()


async def upsert_rule(
    db: AsyncSession,
    key: str,
    enabled: bool,
    config: dict[str, Any],
) -> LifecycleRule:
    """Создаёт или обновляет строку-оверрайд правила. Самокоммитится.

    Возвращает актуальную ORM-строку. Config сохраняется целиком (кабинет
    присылает полный config на PUT).
    """
    rule = await get_rule(db, key)
    if rule is None:
        rule = LifecycleRule(key=key, enabled=bool(enabled), config=config)
        db.add(rule)
    else:
        rule.enabled = bool(enabled)
        rule.config = config

    await db.commit()
    await db.refresh(rule)
    return rule


async def was_sent(
    db: AsyncSession,
    user_id: int,
    rule_key: str,
    occurrence: int,
) -> bool:
    """Отправлялся ли уже этот шаг правила пользователю (дедуп-проверка)."""
    result = await db.execute(
        select(LifecycleMessageLog.id)
        .where(
            LifecycleMessageLog.user_id == user_id,
            LifecycleMessageLog.rule_key == rule_key,
            LifecycleMessageLog.occurrence == occurrence,
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def record_sent(
    db: AsyncSession,
    user_id: int,
    rule_key: str,
    occurrence: int = 1,
) -> None:
    """Фиксирует факт отправки шага правила. Самокоммитится, НИКОГДА не бросает.

    Fire-and-forget как ``crud/campaign_starts.record_campaign_start``: сбой записи
    телеметрии не должен ломать рассылку. UNIQUE(user_id, rule_key, occurrence)
    делает повторную запись no-op (IntegrityError проглатывается) — это и есть
    гарантия дедупа: два параллельных триггера не отправят шаг дважды.
    """
    try:
        db.add(
            LifecycleMessageLog(
                user_id=user_id,
                rule_key=rule_key,
                occurrence=occurrence,
            )
        )
        await db.commit()
    except IntegrityError:
        # Дубликат (user, rule, occurrence) — ожидаемо, шаг уже отмечен.
        await _safe_rollback(db)
        logger.debug(
            'Шаг lifecycle-правила уже был записан, пропускаем',
            user_id=user_id,
            rule_key=rule_key,
            occurrence=occurrence,
        )
    except Exception as exc:
        await _safe_rollback(db)
        logger.warning(
            'Не удалось записать отправку lifecycle-сообщения',
            user_id=user_id,
            rule_key=rule_key,
            occurrence=occurrence,
            error=exc,
        )


async def reserve_send(db: AsyncSession, user_id: int, rule_key: str, occurrence: int) -> bool:
    """Atomically reserve a lifecycle delivery key before external side effects."""
    try:
        db.add(LifecycleMessageLog(user_id=user_id, rule_key=rule_key, occurrence=occurrence))
        await db.commit()
    except IntegrityError:
        await _safe_rollback(db)
        return False
    except Exception as exc:
        await _safe_rollback(db)
        logger.warning(
            'Не удалось зарезервировать отправку lifecycle-сообщения',
            user_id=user_id,
            rule_key=rule_key,
            occurrence=occurrence,
            error=exc,
        )
        return False
    return True


async def release_send_reservation(db: AsyncSession, user_id: int, rule_key: str, occurrence: int) -> None:
    """Release a reservation after a failed external delivery so the job can retry."""
    try:
        await db.execute(
            delete(LifecycleMessageLog).where(
                LifecycleMessageLog.user_id == user_id,
                LifecycleMessageLog.rule_key == rule_key,
                LifecycleMessageLog.occurrence == occurrence,
            )
        )
        await db.commit()
    except Exception as exc:
        await _safe_rollback(db)
        logger.warning(
            'Не удалось освободить резерв lifecycle-сообщения',
            user_id=user_id,
            rule_key=rule_key,
            occurrence=occurrence,
            error=exc,
        )


async def get_sent_counts(db: AsyncSession, rule_keys: list[str]) -> dict[str, int]:
    """Число отправок по каждому правилу ОДНИМ сгруппированным запросом.

    Returns:
        {rule_key: sent_count}. Правила без отправок в словарь не попадают —
        caller подставляет 0.
    """
    if not rule_keys:
        return {}

    result = await db.execute(
        select(LifecycleMessageLog.rule_key, func.count(LifecycleMessageLog.id))
        .where(LifecycleMessageLog.rule_key.in_(rule_keys))
        .group_by(LifecycleMessageLog.rule_key)
    )
    return {row[0]: (row[1] or 0) for row in result.all()}


async def get_email_lifecycle_stats(
    db: AsyncSession,
    event_keys: tuple[str, ...],
    since: datetime,
) -> EmailLifecycleStats:
    """Aggregate lifecycle-email audience and delivery statistics."""
    counts_result = await db.execute(
        select(
            func.count(User.id).filter(User.promo_emails_opt_out_at.is_not(None)),
            func.count(User.id).filter(User.email_verified.is_(True), User.telegram_id.is_(None)),
        )
    )
    optout_count, audience_count = counts_result.one()

    event_conditions = [
        (LifecycleMessageLog.rule_key.like(f'{event_key.removesuffix("_email")}:%\\_email', escape='\\'), event_key)
        for event_key in event_keys
    ]
    delivery_filter = or_(*(condition for condition, _event_key in event_conditions))
    event_key_case = case(*event_conditions)
    totals_result = await db.execute(
        select(event_key_case.label('event_key'), func.count(LifecycleMessageLog.id))
        .where(delivery_filter, LifecycleMessageLog.sent_at >= since)
        .group_by(event_key_case)
    )
    totals_30d = dict.fromkeys(event_keys, 0)
    totals_30d.update({event_key: count for event_key, count in totals_result.all()})

    recent_result = await db.execute(
        select(
            event_key_case.label('event_key'),
            LifecycleMessageLog.user_id,
            User.email,
            LifecycleMessageLog.sent_at,
        )
        .join(User, User.id == LifecycleMessageLog.user_id)
        .where(delivery_filter, User.email.is_not(None))
        .order_by(LifecycleMessageLog.sent_at.desc())
        .limit(_EMAIL_RECENT_LIMIT)
    )
    recent = [
        EmailLifecycleRecentDelivery(event_key=event_key, user_id=user_id, email=email, sent_at=sent_at)
        for event_key, user_id, email, sent_at in recent_result.all()
    ]
    return EmailLifecycleStats(
        optout_count=optout_count or 0,
        audience_count=audience_count or 0,
        totals_30d=totals_30d,
        recent=recent,
    )


async def _safe_rollback(db: AsyncSession) -> None:
    """Откат сессии, который сам никогда не бросает."""
    try:
        await db.rollback()
    except Exception:
        logger.exception('Не удалось откатить сессию после ошибки записи lifecycle-лога')
