"""Discount activation for cohort A of the one-off win-back."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.discount_offer import mark_offer_claimed, upsert_discount_offer
from app.services.winback_oneoff_audience import EVENT_KEY, Target


@dataclass(frozen=True, slots=True)
class DiscountSpec:
    percent: int
    valid_hours: int
    now: datetime


async def activate_discount(db: AsyncSession, target: Target, spec: DiscountSpec) -> datetime:
    offer = await upsert_discount_offer(
        db,
        user_id=target.user.id,
        subscription_id=target.subscription_id,
        notification_type=EVENT_KEY,
        discount_percent=spec.percent,
        bonus_amount_kopeks=0,
        valid_hours=spec.valid_hours,
        effect_type='percent_discount',
    )
    expires_at: datetime = offer.expires_at
    target.user.promo_offer_discount_percent = spec.percent
    target.user.promo_offer_discount_source = EVENT_KEY
    target.user.promo_offer_discount_expires_at = expires_at
    target.user.updated_at = spec.now
    await mark_offer_claimed(
        db,
        offer,
        details={
            'context': 'lifecycle_email_activation',
            'discount_percent': spec.percent,
            'discount_expires_at': expires_at.isoformat(),
        },
    )
    return expires_at
