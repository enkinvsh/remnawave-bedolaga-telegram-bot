from types import SimpleNamespace

import pytest

from app.database.models import UserStatus
from app.handlers.admin.users import UserFilterType, _build_user_button_text


@pytest.mark.parametrize(
    ('status', 'subscriptions', 'expected'),
    [
        (UserStatus.ACTIVE.value, [], '❌ No Subscription'),
        (UserStatus.ACTIVE.value, [SimpleNamespace(is_active=True, is_trial=True)], '🎁 Trial User'),
        (UserStatus.ACTIVE.value, [SimpleNamespace(is_active=True, is_trial=False)], '💎 Paid User'),
        (UserStatus.BLOCKED.value, [SimpleNamespace(is_active=True, is_trial=True)], '🚫 Blocked User'),
    ],
)
def test_user_button_uses_one_semantic_leading_icon(status, subscriptions, expected) -> None:
    user = SimpleNamespace(
        status=status,
        subscriptions=subscriptions,
        full_name=expected.split(' ', 1)[1],
        balance_kopeks=0,
    )

    result = _build_user_button_text(user, UserFilterType.POTENTIAL_CUSTOMERS)

    assert result == expected
