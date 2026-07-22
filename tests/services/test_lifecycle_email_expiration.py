from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.database.models import LifecycleRule
from app.services import lifecycle_email_service as service
from app.services.lifecycle_email_candidates import EmailLifecycleTariff


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


@dataclass(slots=True)
class FixtureUser:
    id: int
    email: str
    promo_emails_opt_out_at: datetime | None
    promo_offer_discount_percent: int
    promo_offer_discount_source: str | None
    promo_offer_discount_expires_at: datetime | None
    updated_at: datetime


@dataclass(slots=True)
class FixtureSubscription:
    id: int
    end_date: datetime
    tariff: EmailLifecycleTariff | None = None


async def test_override_values_survive_commit_between_lifecycle_events(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine('sqlite://')
    LifecycleRule.__table__.create(engine)
    candidate = service.EmailLifecycleCandidate(
        user=FixtureUser(1, 'user@example.com', None, 0, None, None, NOW),
        subscription=FixtureSubscription(10, NOW),
        occurrence=1,
    )
    first_selector = AsyncMock(return_value=[candidate])
    second_selector = AsyncMock(return_value=[])

    try:
        with Session(engine) as orm_session:
            orm_session.add_all(
                [
                    LifecycleRule(key='trial_ending', enabled=True, config={'hours_before': 2}),
                    LifecycleRule(key='post_trial_ladder', enabled=True, config={'steps': []}),
                ]
            )
            orm_session.commit()
            overrides = list(orm_session.scalars(select(LifecycleRule)))

            def reject_implicit_sql(*_args: Any) -> None:
                raise AssertionError('expired lifecycle overrides must not trigger implicit SQL')

            event.listen(engine, 'before_cursor_execute', reject_implicit_sql)

            async def reserve_and_expire(
                _db: AsyncSession,
                _user_id: int,
                _delivery_key: str,
                _occurrence: int,
            ) -> bool:
                orm_session.commit()
                return True

            monkeypatch.setattr(service, 'get_setting_value', AsyncMock(return_value='true'))
            monkeypatch.setattr(service, '_ensure_tracking_campaigns', AsyncMock())
            monkeypatch.setattr(service, 'get_all_rules', AsyncMock(return_value=overrides))
            monkeypatch.setattr(
                service,
                '_EVENTS',
                {
                    'trial_ending_email': (first_selector, AsyncMock(return_value=True)),
                    'post_trial_ladder_email': (second_selector, AsyncMock(return_value=True)),
                },
            )
            monkeypatch.setattr(service, 'reserve_send', reserve_and_expire)

            db = AsyncMock(spec=AsyncSession)
            result = await service.run_lifecycle_emails(db, now=NOW)
    finally:
        engine.dispose()

    assert result == {'trial_ending_email': 1}
    second_selector.assert_awaited_once()
