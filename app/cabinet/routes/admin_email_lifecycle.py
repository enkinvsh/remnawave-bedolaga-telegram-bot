from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.services.email_layout import EMAIL_LAYOUT_ENABLED_KEY
from app.config import settings
from app.database.crud.lifecycle import (
    get_all_rules,
    get_email_lifecycle_stats,
    get_rule,
    upsert_rule,
)
from app.database.crud.system_setting import get_setting_value, upsert_system_setting
from app.database.models import LifecycleRule, User
from app.services import lifecycle_rules
from app.services.lifecycle_email_service import _EVENT_RULES, LIFECYCLE_EMAILS_ENABLED_KEY
from app.services.notification_settings_service import NotificationSettingsService

from ..dependencies import get_cabinet_db, require_permission
from .admin_lifecycle import _validate_config


logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/admin/email-lifecycle', tags=['Admin Email Lifecycle'])
_EMAIL_EVENT_KEYS: Final = tuple(_EVENT_RULES)
_EMAIL_RULE_KEYS: Final = tuple(dict.fromkeys(_EVENT_RULES.values()))


class EmailLifecycleRecent(BaseModel):
    event_key: str
    user_id: int
    email: str
    sent_at: datetime


class EmailLifecycleRuleView(BaseModel):
    key: str
    enabled: bool
    config: dict[str, Any]


class EmailLifecycleOverview(BaseModel):
    enabled: bool
    layout_enabled: bool
    provider: Literal['smtp', 'postbox']
    optout_count: int
    audience_count: int
    totals_30d: dict[str, int]
    recent: list[EmailLifecycleRecent]
    rules: list[EmailLifecycleRuleView]


class EmailLifecycleSettingsUpdate(BaseModel):
    enabled: bool


class EmailLifecycleSettingsResponse(BaseModel):
    enabled: bool


class EmailLifecycleRuleUpdate(BaseModel):
    enabled: bool = False
    config: dict[str, Any] = Field(default_factory=dict)


def _mask_email(email: str) -> str:
    local_part, separator, domain = email.partition('@')
    masked_local_part = f'{local_part[:2]}{"*" * max(0, len(local_part) - 2)}'
    return f'{masked_local_part}{separator}{domain}'


def _resolve_rule_view(key: str, override: LifecycleRule | None) -> EmailLifecycleRuleView:
    enabled = bool(override.__dict__['enabled']) if override is not None else lifecycle_rules.default_enabled(key)
    config = lifecycle_rules.merged_config(key, override.__dict__['config'] if override is not None else None)
    return EmailLifecycleRuleView(key=key, enabled=enabled, config=config)


@router.get('/overview', response_model=EmailLifecycleOverview)
async def get_email_lifecycle_overview(
    _admin: User = Depends(require_permission('email_templates:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailLifecycleOverview:
    enabled_value = await get_setting_value(db, LIFECYCLE_EMAILS_ENABLED_KEY)
    layout_value = await get_setting_value(db, EMAIL_LAYOUT_ENABLED_KEY)
    stats = await get_email_lifecycle_stats(db, _EMAIL_EVENT_KEYS, datetime.now(UTC) - timedelta(days=30))
    overrides = {str(rule.__dict__['key']): rule for rule in await get_all_rules(db)}
    return EmailLifecycleOverview(
        enabled=enabled_value is not None and enabled_value.lower() == 'true',
        layout_enabled=layout_value is not None and layout_value.lower() == 'true',
        provider=settings.EMAIL_PROVIDER,
        optout_count=stats.optout_count,
        audience_count=stats.audience_count,
        totals_30d=stats.totals_30d,
        recent=[
            EmailLifecycleRecent(
                event_key=item.event_key,
                user_id=item.user_id,
                email=_mask_email(item.email),
                sent_at=item.sent_at,
            )
            for item in stats.recent
        ],
        rules=[_resolve_rule_view(key, overrides.get(key)) for key in _EMAIL_RULE_KEYS],
    )


@router.put('/settings', response_model=EmailLifecycleSettingsResponse)
async def update_email_lifecycle_settings(
    payload: EmailLifecycleSettingsUpdate,
    admin: User = Depends(require_permission('email_templates:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailLifecycleSettingsResponse:
    _ = await upsert_system_setting(db, LIFECYCLE_EMAILS_ENABLED_KEY, str(payload.enabled).lower())
    await db.commit()
    logger.info('Admin set lifecycle emails', telegram_id=admin.telegram_id, enabled=payload.enabled)
    return EmailLifecycleSettingsResponse(enabled=payload.enabled)


@router.put('/rules/{key}', response_model=EmailLifecycleRuleView)
async def update_email_lifecycle_rule(
    key: str,
    payload: EmailLifecycleRuleUpdate,
    admin: User = Depends(require_permission('email_templates:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailLifecycleRuleView:
    if key not in _EMAIL_RULE_KEYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Unknown email lifecycle rule '{key}'")

    override = await get_rule(db, key)
    enabled = payload.enabled if 'enabled' in payload.model_fields_set else _resolve_rule_view(key, override).enabled
    config = (
        payload.config
        if 'config' in payload.model_fields_set
        else (override.__dict__['config'] if override is not None else {})
    )
    _validate_config(config)
    rule = await upsert_rule(db, key, enabled, config)
    await NotificationSettingsService.reload(db)
    logger.info('Admin updated email lifecycle rule', telegram_id=admin.telegram_id, key=key, enabled=enabled)
    return _resolve_rule_view(key, rule)
