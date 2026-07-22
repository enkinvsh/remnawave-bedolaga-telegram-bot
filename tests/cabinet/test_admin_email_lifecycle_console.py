from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.cabinet.routes import admin_email_lifecycle as lifecycle_api, router
from app.services import email_lifecycle_analytics
from app.services.winback_oneoff_audience import Audience, Cohort, EmailTarget, TelegramTarget


def _find_route(path: str, method: str):
    return next(
        (
            route
            for route in router.routes
            if getattr(route, 'path', None) == path and method in (getattr(route, 'methods', None) or set())
        ),
        None,
    )


async def test_winback_remaining_excludes_marked_users(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(id=1)
    audiences = {
        Cohort.A: Audience((EmailTarget(user, None, 'a@example.com'),), 2, Cohort.A),
        Cohort.B: Audience((TelegramTarget(user, None, 123),), 1, Cohort.B),
        Cohort.C: Audience((), 3, Cohort.C),
        Cohort.D: Audience((), 4, Cohort.D),
    }
    monkeypatch.setattr(
        email_lifecycle_analytics,
        'select_audience',
        AsyncMock(side_effect=lambda _db, _now, cohort: audiences[cohort]),
    )
    monkeypatch.setattr(email_lifecycle_analytics, '_count_today_winback_markers', AsyncMock(return_value=5))
    monkeypatch.setattr(lifecycle_api.settings, 'POSTBOX_DAILY_QUOTA', 200)

    response = await lifecycle_api.get_email_lifecycle_winback(MagicMock(), AsyncMock())

    assert response.model_dump() == {
        'cohorts': [
            {'cohort': 'a', 'touched': 2, 'remaining_email': 1, 'remaining_telegram': 0},
            {'cohort': 'b', 'touched': 1, 'remaining_email': 0, 'remaining_telegram': 1},
            {'cohort': 'c', 'touched': 3, 'remaining_email': 0, 'remaining_telegram': 0},
            {'cohort': 'd', 'touched': 4, 'remaining_email': 0, 'remaining_telegram': 0},
        ],
        'email_sent_today': 5,
        'daily_quota': 200,
        'email_sent_today_note': 'Прокси: все winback-маркеры за сегодня; канал доставки в журнале не хранится.',
    }


def test_console_routes_use_existing_read_and_edit_permissions() -> None:
    assert _find_route('/cabinet/admin/email-lifecycle/funnel', 'GET') is not None
    assert _find_route('/cabinet/admin/email-lifecycle/timeline', 'GET') is not None
    assert _find_route('/cabinet/admin/email-lifecycle/winback', 'GET') is not None
    assert _find_route('/cabinet/admin/email-lifecycle/test-send', 'POST') is not None


@pytest.mark.parametrize(
    'event',
    ['trial_ending', 'post_trial', 'winback_wave', 'winback_trial_invite', 'winback_trial_reset', 'topup'],
)
async def test_test_send_renders_each_event_and_prefixes_subject(
    monkeypatch: pytest.MonkeyPatch,
    event: str,
) -> None:
    render = AsyncMock(return_value=SimpleNamespace(subject='Тема', html='<p>body</p>', text='body'))
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(lifecycle_api, 'render_test_email', render)
    monkeypatch.setattr(lifecycle_api, 'send_email', deliver)
    lifecycle_api._TEST_SEND_ATTEMPTS.clear()
    admin = MagicMock(id=77)

    response = await lifecycle_api.send_lifecycle_test_email(
        lifecycle_api.EmailLifecycleTestSendRequest(event=event, to='admin@example.com'),
        admin,
        AsyncMock(),
    )

    assert response.model_dump() == {'sent': True}
    render.assert_awaited_once()
    deliver.assert_awaited_once_with(
        to='admin@example.com',
        subject='[ТЕСТ] Тема',
        html='<p>body</p>',
        text='body',
    )


def test_test_send_rejects_invalid_event_and_email() -> None:
    with pytest.raises(ValidationError):
        lifecycle_api.EmailLifecycleTestSendRequest(event='unknown', to='admin@example.com')
    with pytest.raises(ValidationError):
        lifecycle_api.EmailLifecycleTestSendRequest(event='topup', to='not-an-email')


async def test_test_send_rejects_eleventh_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime.now(UTC).date()
    lifecycle_api._TEST_SEND_ATTEMPTS.clear()
    lifecycle_api._TEST_SEND_ATTEMPTS[(77, today)] = 10
    monkeypatch.setattr(lifecycle_api, 'render_test_email', AsyncMock())

    with pytest.raises(lifecycle_api.HTTPException) as exc:
        await lifecycle_api.send_lifecycle_test_email(
            lifecycle_api.EmailLifecycleTestSendRequest(event='topup', to='admin@example.com'),
            MagicMock(id=77),
            AsyncMock(),
        )

    assert exc.value.status_code == 429
