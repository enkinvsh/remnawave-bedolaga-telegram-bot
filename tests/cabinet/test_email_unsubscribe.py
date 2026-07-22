from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.cabinet.routes import email_unsubscribe
from app.cabinet.services.email_unsubscribe import create_unsubscribe_token, verify_unsubscribe_token
from app.config import settings


def test_unsubscribe_token_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'test-secret')

    token = create_unsubscribe_token(42)

    assert verify_unsubscribe_token(token) == 42
    assert verify_unsubscribe_token(f'{token}tampered') is None


@pytest.mark.parametrize('handler', [email_unsubscribe.unsubscribe_get, email_unsubscribe.unsubscribe_post])
async def test_unsubscribe_get_and_post_are_idempotent(handler, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, 'CABINET_JWT_SECRET', 'test-secret')
    user = SimpleNamespace(id=42, promo_emails_opt_out_at=None)
    result = SimpleNamespace(scalar_one_or_none=lambda: user)
    db = AsyncMock()
    db.execute.return_value = result
    token = create_unsubscribe_token(user.id)

    first = await handler(token=token, db=db)
    first_opt_out_at = user.promo_emails_opt_out_at
    second = await handler(token=token, db=db)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first_opt_out_at is not None
    assert first_opt_out_at.tzinfo == UTC
    assert user.promo_emails_opt_out_at == first_opt_out_at
    assert db.commit.await_count == 1
