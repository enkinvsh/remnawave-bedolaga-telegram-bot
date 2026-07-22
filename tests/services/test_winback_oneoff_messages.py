from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from app.services import winback_oneoff_messages as messages
from app.services.winback_oneoff_audience import Cohort


@pytest.mark.parametrize(
    ('cohort', 'body'),
    [
        pytest.param(
            Cohort.B,
            '<p>Мы обновили сервис. Пробный период ждёт активации — это займёт меньше минуты.</p>',
            id='trial-invite',
        ),
        pytest.param(
            Cohort.C,
            '<p>Мы обновили сервис и сбросили ваш пробный период — его можно активировать заново.</p>',
            id='cold-reset',
        ),
        pytest.param(
            Cohort.D,
            '<p>Мы обновили сервис и сбросили ваш пробный период — его можно активировать заново.</p>',
            id='warm-reset',
        ),
    ],
)
async def test_trial_email_uses_required_copy_and_tracking(
    monkeypatch: pytest.MonkeyPatch, cohort: Cohort, body: str
) -> None:
    renderer = AsyncMock(return_value='<html>branded</html>')
    tracking_url = MagicMock(
        return_value=(
            'https://cabinet.example?campaign=winback_trial&utm_source=email'
            '&utm_medium=email&utm_campaign=winback_trial'
        )
    )
    monkeypatch.setattr(messages, 'render_branded_email', renderer)
    monkeypatch.setattr(messages, '_tracked_cabinet_url', tracking_url)
    monkeypatch.setattr(messages, '_unsubscribe_html', MagicMock(return_value='<p>unsubscribe</p>'))

    rendered = await messages.render_trial_email(AsyncMock(), cohort, 42)

    assert rendered.subject == 'Новый пробный период доступен'
    renderer.assert_awaited_once_with(
        ANY,
        title='Новый пробный период доступен',
        body_html=body,
        cta_text='Активировать пробный период',
        cta_url=(
            'https://cabinet.example?campaign=winback_trial&utm_source=email'
            '&utm_medium=email&utm_campaign=winback_trial'
        ),
        unsubscribe_html='<p>unsubscribe</p>',
    )
    tracking_url.assert_called_once_with('winback_trial')
