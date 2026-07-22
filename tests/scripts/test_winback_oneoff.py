from datetime import UTC, datetime, timedelta
from typing import Self
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import User
from app.services import winback_oneoff as campaign


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def _user(
    user_id: int,
    *,
    email: str | None = None,
    email_verified: bool = False,
    opted_out: bool = False,
    telegram_id: int | None = None,
) -> User:
    return User(
        id=user_id,
        email=email,
        email_verified=email_verified,
        promo_emails_opt_out_at=NOW if opted_out else None,
        telegram_id=telegram_id,
        status='active',
        promo_offer_discount_percent=0,
        promo_offer_discount_source=None,
        promo_offer_discount_expires_at=None,
        updated_at=NOW,
    )


def _db_rows(rows: list[tuple[User, int | None, bool]]) -> AsyncMock:
    result = MagicMock()
    result.all.return_value = rows
    db = AsyncMock()
    db.execute.return_value = result
    return db


class _FakeBot:
    def __init__(self) -> None:
        self.send_message = AsyncMock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args) -> None:
        return None


async def test_audience_query_splits_cohorts_and_excludes_ineligible_users() -> None:
    both = _user(1, email='both@example.com', email_verified=True, telegram_id=1001)
    opted_out = _user(2, email='optout@example.com', email_verified=True, opted_out=True, telegram_id=1002)
    telegram = _user(3, telegram_id=1003)
    no_channel = _user(4, email='unverified@example.com')
    already_sent = _user(5, email='sent@example.com', email_verified=True)
    db = _db_rows(
        [
            (both, 11, False),
            (opted_out, 12, False),
            (telegram, None, False),
            (no_channel, 14, False),
            (already_sent, 15, True),
        ]
    )

    audience = await campaign.select_audience(db, NOW)

    assert [(target.user.id, target.channel) for target in audience.targets] == [
        (1, campaign.TargetChannel.EMAIL),
        (2, campaign.TargetChannel.TELEGRAM),
        (3, campaign.TargetChannel.TELEGRAM),
    ]
    assert audience.already_sent == 1
    statement = db.execute.await_args.args[0]
    sql = str(statement.compile(compile_kwargs={'literal_binds': True})).lower()
    assert "deposits.type = 'deposit'" in sql
    assert 'deposits.is_completed is true' in sql
    assert "users.status = 'active'" in sql
    assert "active_subscriptions.status in ('active', 'limited')" in sql
    assert 'active_subscriptions.is_trial is false' in sql
    assert "winback_logs.rule_key like 'winback%'" in sql
    assert "recent_deposits.created_at >= '2026-06-24 12:00:00+00:00'" in sql


async def test_dry_run_performs_zero_writes_or_sends(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    audience = campaign.Audience(
        targets=(
            campaign.EmailTarget(_user(1, email='person@example.com', email_verified=True), 10, 'person@example.com'),
        ),
        already_sent=3,
    )
    db = AsyncMock()
    monkeypatch.setattr(campaign, 'select_audience', AsyncMock(return_value=audience))
    monkeypatch.setattr(
        campaign,
        'render_campaign_email',
        AsyncMock(return_value=campaign.RenderedEmail('Тема', '<html>' + ('x' * 250), 'text')),
    )
    reserve = AsyncMock()
    activate = AsyncMock()
    email_sender = AsyncMock()
    monkeypatch.setattr(campaign, 'reserve_send', reserve)
    monkeypatch.setattr(campaign, 'activate_discount', activate)
    monkeypatch.setattr(campaign, 'send_email', email_sender)

    result = await campaign.run_campaign(db, campaign.Options(cohort=campaign.CohortSelection.A))

    assert result == campaign.RunResult(selected=1, sent=0, skipped_reserved=0, preview_sent=False)
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
    reserve.assert_not_awaited()
    activate.assert_not_awaited()
    email_sender.assert_not_awaited()
    assert capsys.readouterr().out == (
        'DRY-RUN winback_oneoff\n'
        'cohort_a_email=1\n'
        'cohort_a_telegram=0\n'
        'cohort_a_already_sent_skipped=3\n'
        'planned_resets=0\n'
        'selected_targets=1\n'
        'sample[1] cohort=a user_id=***1 channel=email email=p***@example.com telegram_id=-\n'
        'email_subject=Тема\n'
        f"email_html_first_200=<html>{'x' * 194}\n"
        'No sends, resets, or offers written. Use --apply to execute.\n'
    )


async def test_apply_honors_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    targets = tuple(
        campaign.EmailTarget(
            _user(index, email=f'user{index}@example.com', email_verified=True), index, f'user{index}@example.com'
        )
        for index in range(1, 4)
    )
    bot = _FakeBot()
    monkeypatch.setattr(campaign, 'select_audience', AsyncMock(return_value=campaign.Audience(targets, 0)))
    monkeypatch.setattr(campaign, 'reserve_send', AsyncMock(return_value=True))
    activate = AsyncMock(return_value=NOW + timedelta(hours=72))
    monkeypatch.setattr(campaign, 'activate_discount', activate)
    monkeypatch.setattr(campaign, 'render_campaign_email', AsyncMock(return_value=campaign.RenderedEmail('s', 'h', 't')))
    email_sender = AsyncMock(return_value=True)
    monkeypatch.setattr(campaign, 'send_email', email_sender)
    monkeypatch.setattr(campaign, 'create_bot', MagicMock(return_value=bot))
    monkeypatch.setattr(campaign.anyio, 'sleep', AsyncMock())

    result = await campaign.run_campaign(
        AsyncMock(), campaign.Options(apply=True, limit=2, cohort=campaign.CohortSelection.A)
    )

    assert result.sent == 2
    assert activate.await_count == 2
    assert email_sender.await_count == 2


async def test_reserved_marker_prevents_resend(monkeypatch: pytest.MonkeyPatch) -> None:
    target = campaign.EmailTarget(_user(1, email='sent@example.com', email_verified=True), 10, 'sent@example.com')
    monkeypatch.setattr(campaign, 'select_audience', AsyncMock(return_value=campaign.Audience((target,), 0)))
    monkeypatch.setattr(campaign, 'reserve_send', AsyncMock(return_value=False))
    activate = AsyncMock()
    email_sender = AsyncMock()
    monkeypatch.setattr(campaign, 'activate_discount', activate)
    monkeypatch.setattr(campaign, 'send_email', email_sender)
    monkeypatch.setattr(campaign, 'create_bot', MagicMock(return_value=_FakeBot()))

    result = await campaign.run_campaign(
        AsyncMock(), campaign.Options(apply=True, cohort=campaign.CohortSelection.A)
    )

    assert result == campaign.RunResult(selected=1, sent=0, skipped_reserved=1, preview_sent=False)
    activate.assert_not_awaited()
    email_sender.assert_not_awaited()


async def test_preview_sends_one_email_without_audience_or_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    db = AsyncMock()
    select_audience = AsyncMock()
    activate = AsyncMock()
    email_sender = AsyncMock(return_value=True)
    monkeypatch.setattr(campaign, 'select_audience', select_audience)
    monkeypatch.setattr(campaign, 'activate_discount', activate)
    monkeypatch.setattr(campaign, 'send_email', email_sender)
    monkeypatch.setattr(
        campaign,
        'render_campaign_email',
        AsyncMock(return_value=campaign.RenderedEmail('subject', '<html>', 'text')),
    )

    result = await campaign.run_campaign(db, campaign.Options(preview_to='operator@example.com'))

    assert result.preview_sent is True
    select_audience.assert_not_awaited()
    activate.assert_not_awaited()
    email_sender.assert_awaited_once_with(
        to='operator@example.com', subject='subject', html='<html>', text='text', headers=None
    )


async def test_telegram_uses_menu_buy_button(monkeypatch: pytest.MonkeyPatch) -> None:
    target = campaign.TelegramTarget(_user(1, telegram_id=1001), None, 1001)
    bot = _FakeBot()
    monkeypatch.setattr(campaign, 'select_audience', AsyncMock(return_value=campaign.Audience((target,), 0)))
    monkeypatch.setattr(campaign, 'reserve_send', AsyncMock(return_value=True))
    monkeypatch.setattr(campaign, 'activate_discount', AsyncMock(return_value=NOW + timedelta(hours=72)))
    monkeypatch.setattr(campaign, 'create_bot', MagicMock(return_value=bot))

    result = await campaign.run_campaign(
        AsyncMock(), campaign.Options(apply=True, cohort=campaign.CohortSelection.A)
    )

    assert result.sent == 1
    send_call = bot.send_message.await_args
    assert send_call is not None
    button = send_call.kwargs['reply_markup'].inline_keyboard[0][0]
    assert button.callback_data == 'menu_buy'

