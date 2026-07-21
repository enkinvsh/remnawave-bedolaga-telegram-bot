"""CRUD для lifecycle-правил и журнала отправок.

``lifecycle_rules`` — конфиг-оверрайды поверх реестра ``services/lifecycle_rules``.
``lifecycle_message_log`` — дедуп-журнал (одна строка = один отправленный шаг).
"""

from typing import Any

import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LifecycleMessageLog, LifecycleRule


logger = structlog.get_logger(__name__)


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


async def _safe_rollback(db: AsyncSession) -> None:
    """Откат сессии, который сам никогда не бросает."""
    try:
        await db.rollback()
    except Exception:
        logger.exception('Не удалось откатить сессию после ошибки записи lifecycle-лога')
