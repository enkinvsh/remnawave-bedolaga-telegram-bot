import sys
from datetime import UTC, datetime, timedelta
from importlib import import_module
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import NullPool

from app.database.crud import discount_offer as discount_offer_crud, lifecycle as lifecycle_crud
from app.database.models import (
    DiscountOffer,
    LifecycleMessageLog,
    LifecycleRule,
    Subscription,
    SubscriptionStatus,
    User,
    UserStatus,
)
from app.services import lifecycle_email_service as service
from app.services.lifecycle_email_candidates import EmailLifecycleCandidate


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


@compiles(JSONB, 'sqlite')
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    return 'JSON'


async def test_duplicate_reservation_does_not_expire_next_real_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, 'aiosqlite')
    import_module('aiosqlite')
    engine = create_async_engine(
        'sqlite+aiosqlite:///file:lifecycle_email_expiry?mode=memory&cache=shared&uri=true',
        poolclass=NullPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    delivered_subscription_ids: list[int] = []

    async def sender(
        _db: AsyncSession,
        candidate: EmailLifecycleCandidate,
        _config: dict[str, Any],
        _now: datetime,
    ) -> bool:
        delivered_subscription_ids.append(candidate.subscription.id)
        return True

    monkeypatch.setattr(service, 'get_setting_value', AsyncMock(return_value='true'))
    monkeypatch.setattr(service, '_ensure_tracking_campaigns', AsyncMock())
    monkeypatch.setattr(service, '_EVENTS', {'trial_ending_email': (service.select_trial_ending_email, sender)})
    monkeypatch.setattr(lifecycle_crud, 'AsyncSessionLocal', session_factory, raising=False)

    try:
        async with engine.connect() as keeper:
            for table in (
                User.__table__,
                Subscription.__table__,
                LifecycleRule.__table__,
                LifecycleMessageLog.__table__,
            ):
                await keeper.run_sync(table.create)
            await keeper.commit()

            async with session_factory() as db:
                db.add_all(
                    [
                        User(
                            id=1,
                            email='first@example.com',
                            email_verified=True,
                            telegram_id=None,
                            status=UserStatus.ACTIVE.value,
                        ),
                        User(
                            id=2,
                            email='second@example.com',
                            email_verified=True,
                            telegram_id=None,
                            status=UserStatus.ACTIVE.value,
                        ),
                        Subscription(
                            id=10,
                            user_id=1,
                            status=SubscriptionStatus.TRIAL.value,
                            is_trial=True,
                            end_date=NOW + timedelta(hours=1),
                            remnawave_short_id='first',
                        ),
                        Subscription(
                            id=20,
                            user_id=2,
                            status=SubscriptionStatus.TRIAL.value,
                            is_trial=True,
                            end_date=NOW + timedelta(hours=1),
                            remnawave_short_id='second',
                        ),
                        LifecycleRule(key='trial_ending', enabled=True, config={'hours_before': 2}),
                        LifecycleMessageLog(user_id=1, rule_key='trial_ending:10_email', occurrence=1),
                    ]
                )
                await db.commit()

                result = await service.run_lifecycle_emails(db, now=NOW)
    finally:
        await engine.dispose()

    assert result == {'trial_ending_email': 1}
    assert delivered_subscription_ids == [20]


async def test_claim_log_failure_does_not_rollback_caller_session(monkeypatch: pytest.MonkeyPatch) -> None:
    caller_db = AsyncMock(spec=AsyncSession)
    log_db = AsyncMock(spec=AsyncSession)
    log_session_context = AsyncMock()
    log_session_context.__aenter__.return_value = log_db
    log_session_factory = MagicMock(return_value=log_session_context)
    offer = DiscountOffer(
        id=1,
        user_id=1,
        subscription_id=10,
        notification_type='post_trial_ladder',
        discount_percent=10,
        bonus_amount_kopeks=0,
        expires_at=NOW + timedelta(hours=24),
        effect_type='percent_discount',
        is_active=True,
    )

    monkeypatch.setattr(discount_offer_crud, 'AsyncSessionLocal', log_session_factory, raising=False)
    monkeypatch.setattr(
        discount_offer_crud,
        'log_promo_offer_action',
        AsyncMock(side_effect=RuntimeError('telemetry unavailable')),
    )

    result = await discount_offer_crud.mark_offer_claimed(caller_db, offer)

    assert result is offer
    caller_db.rollback.assert_not_awaited()
