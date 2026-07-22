"""Safe trial reset for win-back delivery."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database.crud.subscription import is_active_paid_subscription, wipe_trial_subscriptions
from app.database.models import Subscription


async def reset_trial(db: AsyncSession, user_id: int) -> bool:
    result = await db.execute(
        select(Subscription).options(selectinload(Subscription.user)).where(Subscription.user_id == user_id)
    )
    subscriptions = result.scalars().unique().all()
    trial_subscriptions = [subscription for subscription in subscriptions if subscription.is_trial is True]
    paid_subscriptions = [subscription for subscription in subscriptions if subscription.is_trial is not True]
    subscriptions_to_wipe = (
        trial_subscriptions if settings.is_multi_tariff_enabled() and paid_subscriptions else subscriptions
    )
    if not subscriptions_to_wipe or any(is_active_paid_subscription(item) for item in subscriptions_to_wipe):
        return False
    wiped = await wipe_trial_subscriptions(db, subscriptions_to_wipe)
    if wiped == 0:
        return False
    await db.commit()
    return True
