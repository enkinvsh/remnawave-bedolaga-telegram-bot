from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from app.database.models import User
from app.services import winback_oneoff as campaign, winback_oneoff_reset as reset_service
from app.services.winback_oneoff_audience import Cohort


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _user(user_id: int, *, email: str | None = None, telegram_id: int | None = None) -> User:
    return User(
        id=user_id,
        email=email,
        email_verified=email is not None,
        promo_emails_opt_out_at=None,
        telegram_id=telegram_id,
        status='active',
        language='ru',
        promo_offer_discount_percent=0,
        promo_offer_discount_source=None,
        promo_offer_discount_expires_at=None,
        updated_at=NOW,
    )


def _email_audience(cohort) -> campaign.Audience:
    user = _user(1, email='person@example.com')
    target = campaign.EmailTarget(user, 10, 'person@example.com')
    return campaign.Audience((target,), 0, cohort)


class _FakeBot:
    def __init__(self) -> None:
        self.send_message = AsyncMock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args) -> None:
        return None


@pytest.fixture(autouse=True)
def _mock_trial_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        campaign,
        'render_trial_email',
        AsyncMock(return_value=campaign.RenderedEmail('Тема триала', '<html>trial</html>', 'trial')),
    )


def _reset_db(*subscriptions: SimpleNamespace) -> AsyncMock:
    result = MagicMock()
    result.scalars.return_value.unique.return_value.all.return_value = list(subscriptions)
    db = AsyncMock()
    db.execute.return_value = result
    return db


async def test_trial_dry_run_plans_reset_without_writes_or_external_calls(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cohort = Cohort.C
    db = AsyncMock()
    monkeypatch.setattr(campaign, 'select_audience', AsyncMock(return_value=_email_audience(cohort)))
    reserve = AsyncMock()
    wipe = AsyncMock()
    email_sender = AsyncMock()
    monkeypatch.setattr(campaign, 'reserve_send', reserve)
    monkeypatch.setattr(reset_service, 'wipe_trial_subscriptions', wipe)
    monkeypatch.setattr(campaign, 'send_email', email_sender)

    result = await campaign.run_campaign(db, campaign.Options(cohort=campaign.CohortSelection.C), now=NOW)

    assert result.selected == 1
    assert result.resets == 0
    reserve.assert_not_awaited()
    wipe.assert_not_awaited()
    email_sender.assert_not_awaited()
    db.commit.assert_not_awaited()
    output = capsys.readouterr().out
    assert 'cohort_c_email=1' in output
    assert 'cohort_c_telegram=0' in output
    assert 'cohort_c_already_sent_skipped=0' in output
    assert 'planned_resets=1' in output


@pytest.mark.parametrize('cohort', [Cohort.C, Cohort.D])
async def test_apply_resets_cold_and_warm_trials_before_email(monkeypatch: pytest.MonkeyPatch, cohort) -> None:
    events: list[str] = []
    trial = SimpleNamespace(
        id=10,
        user_id=1,
        user=SimpleNamespace(id=1, remnawave_uuid=None),
        is_trial=True,
        status='expired',
        end_date=NOW - timedelta(days=1),
    )
    db = _reset_db(trial)
    monkeypatch.setattr(campaign, 'select_audience', AsyncMock(return_value=_email_audience(cohort)))
    monkeypatch.setattr(campaign, 'reserve_send', AsyncMock(return_value=True))
    monkeypatch.setattr(type(reset_service.settings), 'is_multi_tariff_enabled', MagicMock(return_value=False))

    async def wipe(*_args) -> int:
        events.append('reset')
        return 1

    async def send_email(**_kwargs) -> bool:
        events.append('send')
        return True

    monkeypatch.setattr(reset_service, 'wipe_trial_subscriptions', wipe)
    monkeypatch.setattr(campaign, 'send_email', send_email)
    monkeypatch.setattr(campaign, 'create_bot', MagicMock(return_value=_FakeBot()))

    result = await campaign.run_campaign(
        db,
        campaign.Options(apply=True, cohort=campaign.CohortSelection(cohort.value)),
        now=NOW,
    )

    assert events == ['reset', 'send']
    assert result.resets == 1
    assert result.sent == 1
    db.commit.assert_awaited_once()


async def test_trial_invite_never_resets_or_activates_discount(monkeypatch: pytest.MonkeyPatch) -> None:
    cohort = Cohort.B
    monkeypatch.setattr(campaign, 'select_audience', AsyncMock(return_value=_email_audience(cohort)))
    monkeypatch.setattr(campaign, 'reserve_send', AsyncMock(return_value=True))
    wipe = AsyncMock()
    activate = AsyncMock()
    monkeypatch.setattr(reset_service, 'wipe_trial_subscriptions', wipe)
    monkeypatch.setattr(campaign, 'activate_discount', activate)
    monkeypatch.setattr(campaign, 'send_email', AsyncMock(return_value=True))
    monkeypatch.setattr(campaign, 'create_bot', MagicMock(return_value=_FakeBot()))

    result = await campaign.run_campaign(
        AsyncMock(), campaign.Options(apply=True, cohort=campaign.CohortSelection.B), now=NOW
    )

    assert result.sent == 1
    wipe.assert_not_awaited()
    activate.assert_not_awaited()


async def test_reset_safety_skips_user_if_active_paid_subscription_would_be_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cohort = Cohort.C
    user = SimpleNamespace(id=1, remnawave_uuid=None)
    trial = SimpleNamespace(
        id=10,
        user_id=1,
        user=user,
        is_trial=True,
        status='expired',
        end_date=NOW - timedelta(days=1),
    )
    paid = SimpleNamespace(
        id=11,
        user_id=1,
        user=user,
        is_trial=False,
        status='active',
        end_date=NOW + timedelta(days=365),
    )
    db = _reset_db(trial, paid)
    monkeypatch.setattr(campaign, 'select_audience', AsyncMock(return_value=_email_audience(cohort)))
    monkeypatch.setattr(campaign, 'reserve_send', AsyncMock(return_value=True))
    monkeypatch.setattr(type(reset_service.settings), 'is_multi_tariff_enabled', MagicMock(return_value=False))
    wipe = AsyncMock()
    email_sender = AsyncMock()
    monkeypatch.setattr(reset_service, 'wipe_trial_subscriptions', wipe)
    monkeypatch.setattr(campaign, 'send_email', email_sender)
    monkeypatch.setattr(campaign, 'create_bot', MagicMock(return_value=_FakeBot()))

    result = await campaign.run_campaign(db, campaign.Options(apply=True, cohort=campaign.CohortSelection.C), now=NOW)

    assert result.skipped_reset == 1
    assert result.sent == 0
    wipe.assert_not_awaited()
    email_sender.assert_not_awaited()


@pytest.mark.parametrize(
    ('cohort', 'event_key'),
    [
        pytest.param(Cohort.A, 'winback_oneoff', id='a'),
        pytest.param(Cohort.B, 'winback_trial_b', id='b'),
        pytest.param(Cohort.C, 'winback_trial_c', id='c'),
        pytest.param(Cohort.D, 'winback_trial_d', id='d'),
    ],
)
async def test_lifecycle_reservation_is_scoped_per_cohort(
    monkeypatch: pytest.MonkeyPatch, cohort, event_key: str
) -> None:
    reserve = AsyncMock(return_value=False)
    monkeypatch.setattr(campaign, 'select_audience', AsyncMock(return_value=_email_audience(cohort)))
    monkeypatch.setattr(campaign, 'reserve_send', reserve)
    monkeypatch.setattr(campaign, 'create_bot', MagicMock(return_value=_FakeBot()))
    wipe = AsyncMock()
    monkeypatch.setattr(reset_service, 'wipe_trial_subscriptions', wipe)

    result = await campaign.run_campaign(
        AsyncMock(),
        campaign.Options(apply=True, cohort=campaign.CohortSelection(cohort.value)),
        now=NOW,
    )

    reserve.assert_awaited_once_with(ANY, 1, event_key, 1)
    assert result.skipped_reserved == 1
    wipe.assert_not_awaited()


async def test_cohort_filter_queries_only_requested_cohort(monkeypatch: pytest.MonkeyPatch) -> None:
    selector = AsyncMock(return_value=campaign.Audience((), 0, Cohort.D))
    monkeypatch.setattr(campaign, 'select_audience', selector)

    await campaign.run_campaign(AsyncMock(), campaign.Options(cohort=campaign.CohortSelection.D), now=NOW)

    selector.assert_awaited_once()
    select_call = selector.await_args
    assert select_call is not None
    assert select_call.args[2] is Cohort.D


async def test_trial_telegram_uses_trial_activation_keyboard(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user(1, telegram_id=1001)
    target = campaign.TelegramTarget(user, None, 1001)
    bot = _FakeBot()
    monkeypatch.setattr(
        campaign,
        'select_audience',
        AsyncMock(return_value=campaign.Audience((target,), 0, Cohort.B)),
    )
    monkeypatch.setattr(campaign, 'reserve_send', AsyncMock(return_value=True))
    monkeypatch.setattr(campaign, 'create_bot', MagicMock(return_value=bot))

    result = await campaign.run_campaign(
        AsyncMock(), campaign.Options(apply=True, cohort=campaign.CohortSelection.B), now=NOW
    )

    assert result.sent == 1
    send_call = bot.send_message.await_args
    assert send_call is not None
    keyboard = send_call.kwargs['reply_markup']
    assert keyboard.inline_keyboard[0][0].callback_data == 'trial_activate'
