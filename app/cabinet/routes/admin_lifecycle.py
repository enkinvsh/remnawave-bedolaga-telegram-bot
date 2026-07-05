"""Admin lifecycle-notification routes for cabinet.

Читает и пишет конфигурируемые lifecycle-правила (реестр дефолтов
``app/services/lifecycle_rules.py`` + БД-оверрайды ``lifecycle_rules``). Это
фундамент C1: только схема + конфиг + API. Ничего НЕ отправляет — триггеры и
фактическая рассылка — задача C2.
"""

from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.lifecycle import get_all_rules, get_sent_counts, upsert_rule
from app.database.models import User
from app.services import lifecycle_rules
from app.services.notification_settings_service import NotificationSettingsService

from ..dependencies import get_cabinet_db, require_permission


logger = structlog.get_logger(__name__)

router = APIRouter(prefix='/admin/notifications', tags=['Admin Lifecycle Notifications'])

# Верхняя граница для полей-процентов (имя содержит 'percent').
_PERCENT_MAX = 100


# ============ Schemas ============


class LifecycleRuleView(BaseModel):
    """Правило в ответе: дефолт реестра, наложенный оверрайдом из БД."""

    key: str
    group: str
    enabled: bool
    config: dict[str, Any]
    placeholders: list[str]
    sent_count: int


class LifecycleRuleUpdate(BaseModel):
    """Тело PUT: полный набор настроек правила (кабинет знает форму per-key)."""

    enabled: bool
    config: dict[str, Any] = Field(default_factory=dict)


# ============ Helpers ============


def _validate_config(config: dict[str, Any]) -> None:
    """Мелкая (shallow) валидация типов config'а — 422 при нарушении.

    Числа — неотрицательные; поля-проценты (имя содержит 'percent') — 0..100;
    поля-шаблоны (имя содержит 'template') — не число (текст/структура). Вложенные
    списки/словари (steps, offsets_hours, message_templates) глубоко НЕ проверяем —
    их форму знает фронт per-key.
    """
    for field_key, value in config.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            if 'template' in field_key:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Field '{field_key}' must be text, not a number",
                )
            if value < 0:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Field '{field_key}' must be a non-negative number",
                )
            if 'percent' in field_key and value > _PERCENT_MAX:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Field '{field_key}' must be a percent between 0 and {_PERCENT_MAX}",
                )


def _build_view(
    key: str,
    enabled: bool,
    override_config: dict[str, Any] | None,
    sent_count: int,
) -> LifecycleRuleView:
    descriptor = lifecycle_rules.get_descriptor(key)
    assert descriptor is not None  # caller guarantees a known key
    return LifecycleRuleView(
        key=key,
        group=descriptor.group,
        enabled=enabled,
        config=lifecycle_rules.merged_config(key, override_config),
        placeholders=list(descriptor.placeholders),
        sent_count=sent_count,
    )


# ============ Routes ============


@router.get('/lifecycle', response_model=list[LifecycleRuleView])
async def list_lifecycle_rules(
    admin: User = Depends(require_permission('settings:read')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Список всех lifecycle-правил (дефолты реестра + оверрайды БД + счётчик отправок)."""
    overrides = {rule.key: rule for rule in await get_all_rules(db)}
    keys = list(lifecycle_rules.all_keys())
    sent_counts = await get_sent_counts(db, keys)

    views: list[LifecycleRuleView] = []
    for key in keys:
        override = overrides.get(key)
        enabled = bool(override.enabled) if override is not None else lifecycle_rules.default_enabled(key)
        override_config = override.config if override is not None else None
        views.append(_build_view(key, enabled, override_config, sent_counts.get(key, 0)))
    return views


@router.put('/lifecycle/{key}', response_model=LifecycleRuleView)
async def update_lifecycle_rule(
    key: str,
    payload: LifecycleRuleUpdate,
    admin: User = Depends(require_permission('settings:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
):
    """Обновить правило: валидация ключа (404) и config'а (422), upsert, рефреш кэша."""
    if not lifecycle_rules.is_known_key(key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown lifecycle rule '{key}'")

    _validate_config(payload.config)

    rule = await upsert_rule(db, key, payload.enabled, payload.config)

    # Рефреш in-memory кэша сервиса: monitoring_service (тот же процесс) сразу
    # увидит новые значения для управляемых им ключей группы 'paid'.
    await NotificationSettingsService.reload(db)

    sent_counts = await get_sent_counts(db, [key])
    logger.info(
        'Admin updated lifecycle rule',
        telegram_id=admin.telegram_id,
        key=key,
        enabled=bool(rule.enabled),
    )
    return _build_view(key, bool(rule.enabled), rule.config or {}, sent_counts.get(key, 0))
