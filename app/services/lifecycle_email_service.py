from datetime import UTC, datetime
from typing import Any, Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.services.email_layout import EMAIL_SERVICE_CAMPAIGN
from app.config import settings
from app.database.crud.discount_offer import mark_offer_claimed, upsert_discount_offer
from app.database.crud.lifecycle import get_all_rules, release_send_reservation, reserve_send
from app.database.crud.system_setting import get_setting_value
from app.database.models import DiscountOffer
from app.services.email_delivery_service import send_email
from app.services.lifecycle_email_candidates import (
    EmailLifecycleCandidate,
    EmailLifecycleSubscription,
    EmailLifecycleUser,
    select_post_trial_email,
    select_second_winback_email,
    select_third_winback_email,
    select_trial_ending_email,
)
from app.services.lifecycle_email_messages import (
    _promo_headers,
    render_lifecycle_discount_email,
    render_trial_ending_email,
)
from app.services.lifecycle_rules import default_enabled, merged_config


logger = structlog.get_logger(__name__)
LIFECYCLE_EMAILS_ENABLED_KEY: Final = 'CABINET_LIFECYCLE_EMAILS_ENABLED'
_EVENT_RULES: Final = {
    'trial_ending_email': 'trial_ending',
    'post_trial_ladder_email': 'post_trial_ladder',
    'expired_discount_wave2_email': 'expired_second_wave',
    'expired_discount_wave3_email': 'expired_third_wave',
}


_TRACKING_CAMPAIGNS: dict[str, str] = {
    EMAIL_SERVICE_CAMPAIGN: 'Email: сервисные уведомления',
    'email_trial_ending': 'Email: триал заканчивается',
    'email_post_trial': 'Email: лестница после трила',
    'email_winback_w2': 'Email: win-back волна 2',
    'email_winback_w3': 'Email: win-back волна 3',
    'email_referral_block': 'Email: реферальный блок',
    'winback_oneoff': 'Email: win-back разовая кампания',
    'winback_trial': 'Email: новый пробный период',
}

_tracking_campaigns_ensured = False


async def _ensure_tracking_campaigns(db: AsyncSession) -> None:
    global _tracking_campaigns_ensured
    if _tracking_campaigns_ensured:
        return
    from app.database.crud.campaign import create_campaign, get_campaign_by_start_parameter

    for start_parameter, name in _TRACKING_CAMPAIGNS.items():
        existing = await get_campaign_by_start_parameter(db, start_parameter, only_active=False)
        if existing is None:
            await create_campaign(db, name=name, start_parameter=start_parameter, bonus_type='none')
    _tracking_campaigns_ensured = True


def _delivery_key(event_key: str, subscription_id: int) -> str:
    base_key = event_key.removesuffix('_email')
    return f'{base_key}:{subscription_id}_email'


async def activate_email_discount(
    db: AsyncSession,
    *,
    user: EmailLifecycleUser,
    subscription: EmailLifecycleSubscription,
    notification_type: str,
    discount_percent: int,
    valid_hours: int,
    now: datetime,
) -> DiscountOffer:
    offer = await upsert_discount_offer(
        db,
        user_id=user.id,
        subscription_id=subscription.id,
        notification_type=notification_type,
        discount_percent=discount_percent,
        bonus_amount_kopeks=0,
        valid_hours=valid_hours,
        effect_type='percent_discount',
    )
    user.promo_offer_discount_percent = discount_percent
    user.promo_offer_discount_source = notification_type
    expires_at: datetime = offer.expires_at
    user.promo_offer_discount_expires_at = expires_at
    user.updated_at = now
    claimed_offer = await mark_offer_claimed(
        db,
        offer,
        details={
            'context': 'lifecycle_email_activation',
            'discount_percent': discount_percent,
            'discount_expires_at': expires_at.isoformat(),
        },
    )
    await db.refresh(claimed_offer, ['expires_at'])
    return claimed_offer


async def _send_trial_ending(
    db: AsyncSession,
    candidate: EmailLifecycleCandidate,
    config: dict[str, Any],
    _now: datetime,
) -> bool:
    hours_before = int(config.get('hours_before', 2))
    rendered = await render_trial_ending_email(db, hours_before)
    return await send_email(
        to=candidate.user.email,
        subject=rendered.subject,
        html=rendered.html,
        text=rendered.text,
    )


async def _send_discount(
    db: AsyncSession,
    candidate: EmailLifecycleCandidate,
    config: dict[str, Any],
    now: datetime,
    *,
    notification_type: str,
) -> bool:
    step = candidate.step or config
    percent = int(step.get('discount_percent', 0))
    valid_hours = int(step.get('valid_hours', 24))
    if percent <= 0 or candidate.user.promo_emails_opt_out_at is not None:
        return False
    offer = await activate_email_discount(
        db,
        user=candidate.user,
        subscription=candidate.subscription,
        notification_type=notification_type,
        discount_percent=percent,
        valid_hours=valid_hours,
        now=now,
    )
    rendered = await render_lifecycle_discount_email(
        db,
        notification_type=notification_type,
        percent=percent,
        expires_at=offer.expires_at,
        user_id=candidate.user.id,
    )
    return await send_email(
        to=candidate.user.email,
        subject=rendered.subject,
        html=rendered.html,
        text=rendered.text,
        headers=_promo_headers(candidate.user.id),
    )


async def _send_post_trial(
    db: AsyncSession, candidate: EmailLifecycleCandidate, config: dict[str, Any], now: datetime
) -> bool:
    return await _send_discount(db, candidate, config, now, notification_type='post_trial_ladder')


async def _send_second_winback(
    db: AsyncSession, candidate: EmailLifecycleCandidate, config: dict[str, Any], now: datetime
) -> bool:
    return await _send_discount(db, candidate, config, now, notification_type='expired_discount_wave2')


async def _send_third_winback(
    db: AsyncSession, candidate: EmailLifecycleCandidate, config: dict[str, Any], now: datetime
) -> bool:
    return await _send_discount(db, candidate, config, now, notification_type='expired_discount_wave3')


_EVENTS: dict[str, tuple[Any, Any]] = {
    'trial_ending_email': (select_trial_ending_email, _send_trial_ending),
    'post_trial_ladder_email': (select_post_trial_email, _send_post_trial),
    'expired_discount_wave2_email': (select_second_winback_email, _send_second_winback),
    'expired_discount_wave3_email': (select_third_winback_email, _send_third_winback),
}


async def run_lifecycle_emails(db: AsyncSession, now: datetime | None = None) -> dict[str, int]:
    enabled = await get_setting_value(db, LIFECYCLE_EMAILS_ENABLED_KEY)
    if enabled is None or enabled.lower() != 'true':
        return {}
    current_time = now or datetime.now(UTC)
    await _ensure_tracking_campaigns(db)
    overrides = {rule.key: (bool(rule.enabled), rule.config) for rule in await get_all_rules(db)}
    batch_limit = max(1, int(getattr(settings, 'LIFECYCLE_TRIGGERS_BATCH_LIMIT', 100)))
    totals: dict[str, int] = {}
    for event_key, (selector, sender) in _EVENTS.items():
        rule_key = _EVENT_RULES[event_key]
        override = overrides.get(rule_key)
        if not (override[0] if override is not None else default_enabled(rule_key)):
            continue
        config = merged_config(rule_key, override[1] if override is not None else None)
        sent = 0
        offset = 0
        while sent < batch_limit:
            candidates = await selector(db, config, current_time, batch_limit, offset)
            if not candidates:
                break
            for candidate in candidates:
                if sent >= batch_limit:
                    break
                delivery_key = _delivery_key(event_key, candidate.subscription.id)
                if not await reserve_send(db, candidate.user.id, delivery_key, candidate.occurrence):
                    continue
                delivered = False
                try:
                    delivered = await sender(db, candidate, config, current_time)
                finally:
                    if not delivered:
                        await release_send_reservation(db, candidate.user.id, delivery_key, candidate.occurrence)
                if delivered:
                    sent += 1
            offset += len(candidates)
            if len(candidates) < batch_limit:
                break
        if sent:
            totals[event_key] = sent
            logger.info('lifecycle email: отправлены письма', event_key=event_key, count=sent)
    return totals
