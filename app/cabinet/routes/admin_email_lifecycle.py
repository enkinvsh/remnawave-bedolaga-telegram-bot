from datetime import UTC, date, datetime, timedelta
from typing import Any, Final, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
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
from app.services.email_delivery_service import send_email
from app.services.email_lifecycle_analytics import (
    WINBACK_SENT_TODAY_NOTE,
    get_funnel,
    get_timeline,
    get_winback_stats,
)
from app.services.email_lifecycle_test_send import TestSendEvent, render_test_email
from app.services.lifecycle_email_service import _EVENT_RULES, LIFECYCLE_EMAILS_ENABLED_KEY
from app.services.notification_settings_service import NotificationSettingsService
from app.services.winback_oneoff_audience import Cohort

from ..dependencies import get_cabinet_db, require_permission
from .admin_lifecycle import _validate_config


logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/admin/email-lifecycle', tags=['Admin Email Lifecycle'])
_EMAIL_EVENT_KEYS: Final = tuple(_EVENT_RULES)
_EMAIL_RULE_KEYS: Final = tuple(dict.fromkeys(_EVENT_RULES.values()))
_TEST_SEND_DAILY_LIMIT: Final = 10
_TEST_SEND_ATTEMPTS: Final[dict[tuple[int, date], int]] = {}


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


class EmailLifecycleFunnelItem(BaseModel):
    rule_key: str
    touched: int
    trials_after: int
    deposits_after: int
    revenue_after_kopeks: int


class EmailLifecycleFunnelResponse(BaseModel):
    items: list[EmailLifecycleFunnelItem]


class EmailLifecycleTimelineDay(BaseModel):
    date: str
    by_event: dict[str, int]


class EmailLifecycleTimelineResponse(BaseModel):
    days: list[EmailLifecycleTimelineDay]


class EmailLifecycleWinbackCohort(BaseModel):
    cohort: Cohort
    touched: int
    remaining_email: int
    remaining_telegram: int


class EmailLifecycleWinbackResponse(BaseModel):
    cohorts: list[EmailLifecycleWinbackCohort]
    email_sent_today: int
    daily_quota: int | None
    email_sent_today_note: str


class EmailLifecycleTestSendRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: TestSendEvent
    to: EmailStr


class EmailLifecycleTestSendResponse(BaseModel):
    sent: bool


def _mask_email(email: str) -> str:
    local_part, separator, domain = email.partition('@')
    masked_local_part = f'{local_part[:2]}{"*" * max(0, len(local_part) - 2)}'
    return f'{masked_local_part}{separator}{domain}'


def _resolve_rule_view(key: str, override: LifecycleRule | None) -> EmailLifecycleRuleView:
    enabled = bool(override.enabled) if override is not None else lifecycle_rules.default_enabled(key)
    config = lifecycle_rules.merged_config(key, override.config if override is not None else None)
    return EmailLifecycleRuleView(key=key, enabled=enabled, config=config)


@router.get('/funnel', response_model=EmailLifecycleFunnelResponse)
async def get_email_lifecycle_funnel(
    _admin: User = Depends(require_permission('email_templates:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailLifecycleFunnelResponse:
    return EmailLifecycleFunnelResponse(
        items=[
            EmailLifecycleFunnelItem(
                rule_key=item.rule_key,
                touched=item.touched,
                trials_after=item.trials_after,
                deposits_after=item.deposits_after,
                revenue_after_kopeks=item.revenue_after_kopeks,
            )
            for item in await get_funnel(db)
        ]
    )


@router.get('/timeline', response_model=EmailLifecycleTimelineResponse)
async def get_email_lifecycle_timeline(
    days: int = Query(30, ge=1, le=365),
    _admin: User = Depends(require_permission('email_templates:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailLifecycleTimelineResponse:
    return EmailLifecycleTimelineResponse(
        days=[EmailLifecycleTimelineDay(date=item.date, by_event=item.by_event) for item in await get_timeline(db, days)]
    )


@router.get('/winback', response_model=EmailLifecycleWinbackResponse)
async def get_email_lifecycle_winback(
    _admin: User = Depends(require_permission('email_templates:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailLifecycleWinbackResponse:
    stats = await get_winback_stats(db)
    return EmailLifecycleWinbackResponse(
        cohorts=[
            EmailLifecycleWinbackCohort(
                cohort=item.cohort,
                touched=item.touched,
                remaining_email=item.remaining_email,
                remaining_telegram=item.remaining_telegram,
            )
            for item in stats.cohorts
        ],
        email_sent_today=stats.email_sent_today,
        daily_quota=settings.POSTBOX_DAILY_QUOTA,
        email_sent_today_note=WINBACK_SENT_TODAY_NOTE,
    )


@router.post('/test-send', response_model=EmailLifecycleTestSendResponse)
async def send_lifecycle_test_email(
    payload: EmailLifecycleTestSendRequest,
    admin: User = Depends(require_permission('email_templates:edit')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailLifecycleTestSendResponse:
    now = datetime.now(UTC)
    await db.refresh(admin, ['id'])
    admin_id: int = admin.id
    attempt_key = (admin_id, now.date())
    attempts = _TEST_SEND_ATTEMPTS.get(attempt_key, 0)
    if attempts >= _TEST_SEND_DAILY_LIMIT:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail='Дневной лимит тестовых отправок исчерпан')
    _TEST_SEND_ATTEMPTS[attempt_key] = attempts + 1
    rendered = await render_test_email(db, payload.event, admin_id, now)
    sent = await send_email(
        to=str(payload.to),
        subject=f'[ТЕСТ] {rendered.subject}',
        html=rendered.html,
        text=rendered.text,
    )
    return EmailLifecycleTestSendResponse(sent=sent)


@router.get('/overview', response_model=EmailLifecycleOverview)
async def get_email_lifecycle_overview(
    _admin: User = Depends(require_permission('email_templates:read')),
    db: AsyncSession = Depends(get_cabinet_db),
) -> EmailLifecycleOverview:
    enabled_value = await get_setting_value(db, LIFECYCLE_EMAILS_ENABLED_KEY)
    layout_value = await get_setting_value(db, EMAIL_LAYOUT_ENABLED_KEY)
    stats = await get_email_lifecycle_stats(db, _EMAIL_EVENT_KEYS, datetime.now(UTC) - timedelta(days=30))
    overrides = {str(rule.key): rule for rule in await get_all_rules(db)}
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
    await db.refresh(admin, ['telegram_id'])
    admin_telegram_id = admin.telegram_id
    _ = await upsert_system_setting(db, LIFECYCLE_EMAILS_ENABLED_KEY, str(payload.enabled).lower())
    await db.commit()
    logger.info('Admin set lifecycle emails', telegram_id=admin_telegram_id, enabled=payload.enabled)
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

    await db.refresh(admin, ['telegram_id'])
    admin_telegram_id = admin.telegram_id
    override = await get_rule(db, key)
    enabled = payload.enabled if 'enabled' in payload.model_fields_set else _resolve_rule_view(key, override).enabled
    config = (
        payload.config
        if 'config' in payload.model_fields_set
        else (override.config if override is not None else {})
    )
    _validate_config(config)
    rule = await upsert_rule(db, key, enabled, config)
    rule_view = _resolve_rule_view(key, rule)
    await NotificationSettingsService.reload(db)
    logger.info('Admin updated email lifecycle rule', telegram_id=admin_telegram_id, key=key, enabled=enabled)
    return rule_view
