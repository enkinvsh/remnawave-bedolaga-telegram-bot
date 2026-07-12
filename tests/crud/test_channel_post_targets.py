"""Post-target allowlist CRUD — create/preserve/revoke semantics.

Guards the CRITICAL invariant: a NEW post-target row is created with
``is_active=false`` (an active row would silently become a mandatory
subscription-enforcement channel). Upsert on an existing row preserves every
subscription-enforcement field and only flips ``is_post_target``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.crud import required_channel as mod
from app.database.models import RequiredChannel


def _db():
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.mark.asyncio
async def test_upsert_new_row_is_inactive_and_post_target(monkeypatch):
    monkeypatch.setattr(mod, 'get_channel_by_channel_id', AsyncMock(return_value=None))
    db = _db()

    await mod.upsert_post_target(db, channel_id='-1009', title='Grp')

    added = db.add.call_args.args[0]
    assert isinstance(added, RequiredChannel)
    assert added.is_post_target is True
    assert added.is_active is False
    assert added.channel_id == '-1009'
    assert added.title == 'Grp'
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_existing_row_preserves_subscription_fields(monkeypatch):
    existing = RequiredChannel(
        channel_id='-1001',
        channel_link='https://t.me/x',
        title='Existing',
        is_active=True,
        sort_order=5,
        disable_trial_on_leave=True,
        disable_paid_on_leave=True,
        is_post_target=False,
    )
    monkeypatch.setattr(mod, 'get_channel_by_channel_id', AsyncMock(return_value=existing))
    db = _db()

    result = await mod.upsert_post_target(db, channel_id='-1001', title='NEW-IGNORED')

    assert result.is_post_target is True
    # Subscription-enforcement fields untouched.
    assert result.is_active is True
    assert result.sort_order == 5
    assert result.disable_trial_on_leave is True
    assert result.disable_paid_on_leave is True
    assert result.channel_link == 'https://t.me/x'
    # Non-empty title preserved (not overwritten).
    assert result.title == 'Existing'
    db.add.assert_not_called()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_upsert_existing_fills_empty_title(monkeypatch):
    existing = RequiredChannel(channel_id='-1002', title=None, is_active=True, is_post_target=False)
    monkeypatch.setattr(mod, 'get_channel_by_channel_id', AsyncMock(return_value=existing))
    db = _db()

    result = await mod.upsert_post_target(db, channel_id='-1002', title='Filled')
    assert result.title == 'Filled'
    assert result.is_active is True


@pytest.mark.asyncio
async def test_revoke_clears_only_post_target(monkeypatch):
    existing = RequiredChannel(channel_id='-1001', is_active=True, is_post_target=True)
    monkeypatch.setattr(mod, 'get_post_target_by_channel_id', AsyncMock(return_value=existing))
    db = _db()

    ok = await mod.revoke_post_target(db, '-1001')

    assert ok is True
    assert existing.is_post_target is False
    assert existing.is_active is True  # row survives, still active
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_revoke_missing_returns_false(monkeypatch):
    monkeypatch.setattr(mod, 'get_post_target_by_channel_id', AsyncMock(return_value=None))
    db = _db()

    ok = await mod.revoke_post_target(db, '-1001')
    assert ok is False
    db.commit.assert_not_awaited()
