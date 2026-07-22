from unittest.mock import AsyncMock

import pytest

from app.services import lifecycle_email_service as service


async def test_tracking_campaigns_include_winback_trial(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.database.crud import campaign as campaign_crud

    monkeypatch.setattr(service, '_tracking_campaigns_ensured', False)
    monkeypatch.setattr(campaign_crud, 'get_campaign_by_start_parameter', AsyncMock(return_value=None))
    create_campaign = AsyncMock()
    monkeypatch.setattr(campaign_crud, 'create_campaign', create_campaign)

    await service._ensure_tracking_campaigns(AsyncMock())

    assert {
        'name': 'Email: новый пробный период',
        'start_parameter': 'winback_trial',
        'bonus_type': 'none',
    } in [call.kwargs for call in create_campaign.await_args_list]
