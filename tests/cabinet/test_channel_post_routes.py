"""admin_channel_posts routes — auth gating, validation, send flow, allowlist.

House pattern: handlers are invoked directly with mocked ``db``/``admin`` and
monkeypatched service/crud/cache. Auth-gating is verified by inspecting each
route's ``require_permission`` closure (the exact permission string it enforces).
All chat ids are synthetic.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.params import Header

from app.cabinet.routes import admin_channel_posts as mod
from app.cabinet.schemas.channel_posts import ChannelPostButton, ChannelPostRequest


# ── route registration + permission gating ─────────────────────────────────


def _route(path: str, method: str):
    from app.cabinet.routes import router

    for route in router.routes:
        if getattr(route, 'path', None) == path and method in getattr(route, 'methods', set()):
            return route
    raise AssertionError(f'route not found: {method} {path}')


def _perms_for(route) -> set[str]:
    perms: set[str] = set()
    for dep in route.dependant.dependencies:
        call = dep.call
        code = getattr(call, '__code__', None)
        if code and 'permissions' in code.co_freevars:
            idx = code.co_freevars.index('permissions')
            perms |= set(call.__closure__[idx].cell_contents)
    return perms


def test_routes_registered():
    from app.cabinet.routes import router

    index: dict[str, set[str]] = {}
    for route in router.routes:
        path = getattr(route, 'path', None)
        if path and path.startswith('/cabinet/admin/channel-posts'):
            index.setdefault(path, set()).update(getattr(route, 'methods', set()))

    assert '/cabinet/admin/channel-posts' in index
    assert {'GET', 'POST'} <= index['/cabinet/admin/channel-posts']
    assert 'GET' in index['/cabinet/admin/channel-posts/allowlist']
    assert 'POST' in index['/cabinet/admin/channel-posts/allowlist']
    assert 'POST' in index['/cabinet/admin/channel-posts/preview']
    assert 'DELETE' in index['/cabinet/admin/channel-posts/allowlist/{channel_id}']


def test_permission_gating_exact_strings():
    assert _perms_for(_route('/cabinet/admin/channel-posts', 'GET')) == {'channel_posts:read'}
    assert _perms_for(_route('/cabinet/admin/channel-posts/allowlist', 'GET')) == {'channel_posts:read'}
    assert _perms_for(_route('/cabinet/admin/channel-posts/preview', 'POST')) == {'channel_posts:send'}
    assert _perms_for(_route('/cabinet/admin/channel-posts', 'POST')) == {'channel_posts:send'}
    assert _perms_for(_route('/cabinet/admin/channel-posts/allowlist', 'POST')) == {'channel_posts:allowlist'}
    assert _perms_for(_route('/cabinet/admin/channel-posts/allowlist/{channel_id}', 'DELETE')) == {
        'channel_posts:allowlist'
    }


def test_registry_declares_channel_posts():
    from app.services.permission_service import PERMISSION_REGISTRY

    assert PERMISSION_REGISTRY['channel_posts'] == ['read', 'send', 'allowlist']


def test_send_requires_idempotency_key_header():
    sig = inspect.signature(mod.create_channel_post_endpoint)
    param = sig.parameters['idempotency_key']
    assert isinstance(param.default, Header)
    assert param.default.alias == 'Idempotency-Key'
    # Required (no default) → FastAPI returns 422 when the header is absent.
    assert param.default.is_required()


# ── shared doubles ──────────────────────────────────────────────────────────


def _admin():
    return SimpleNamespace(id=7)


def _patch_common(monkeypatch, *, allowlisted=True, can_post=True, rate_limited=False):
    target = SimpleNamespace(channel_id='-1001', title='T', is_post_target=True) if allowlisted else None
    monkeypatch.setattr(mod, 'get_post_target_by_channel_id', AsyncMock(return_value=target))
    monkeypatch.setattr(
        mod,
        'RateLimitCache',
        SimpleNamespace(is_rate_limited=AsyncMock(return_value=rate_limited)),
    )
    monkeypatch.setattr(mod, 'get_channel_post_by_idempotency_key', AsyncMock(return_value=None))
    monkeypatch.setattr(
        mod,
        'resolve_capability',
        AsyncMock(
            return_value={
                'chat_id': '-1001',
                'title': 'T',
                'type': 'channel',
                'username': None,
                'is_allowlisted': True,
                'bot_status': 'administrator',
                'can_post': can_post,
            }
        ),
    )
    log = AsyncMock()
    monkeypatch.setattr(mod, 'PermissionService', SimpleNamespace(log_action=log))
    return log


# ── preview ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_bad_destination_422(monkeypatch):
    _patch_common(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await mod.preview_channel_post(mod.PreviewRequest(destination_id='abc'), db=AsyncMock(), _admin=_admin())
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_preview_not_allowlisted_403(monkeypatch):
    _patch_common(monkeypatch, allowlisted=False)
    with pytest.raises(HTTPException) as exc:
        await mod.preview_channel_post(mod.PreviewRequest(destination_id='-1001'), db=AsyncMock(), _admin=_admin())
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_preview_advisory_response(monkeypatch):
    _patch_common(monkeypatch, can_post=True)
    resp = await mod.preview_channel_post(mod.PreviewRequest(destination_id='-1001'), db=AsyncMock(), _admin=_admin())
    assert resp.can_post is True
    assert resp.is_allowlisted is True


# ── send flow ───────────────────────────────────────────────────────────────


def _valid_request():
    return ChannelPostRequest(
        destination_id='-1001',
        message_text='hi',
        custom_buttons=[ChannelPostButton(label='A', url='https://a.io')],
    )


@pytest.mark.asyncio
async def test_send_bad_destination_422(monkeypatch):
    _patch_common(monkeypatch)
    send_post = AsyncMock()
    monkeypatch.setattr(mod, 'send_post', send_post)
    req = ChannelPostRequest(destination_id='@name', message_text='hi')
    with pytest.raises(HTTPException) as exc:
        await mod.create_channel_post_endpoint(req, idempotency_key='k', db=AsyncMock(), admin=_admin())
    assert exc.value.status_code == 422
    send_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_not_allowlisted_403(monkeypatch):
    _patch_common(monkeypatch, allowlisted=False)
    send_post = AsyncMock()
    monkeypatch.setattr(mod, 'send_post', send_post)
    with pytest.raises(HTTPException) as exc:
        await mod.create_channel_post_endpoint(_valid_request(), idempotency_key='k', db=AsyncMock(), admin=_admin())
    assert exc.value.status_code == 403
    send_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_rate_limited_429(monkeypatch):
    _patch_common(monkeypatch, rate_limited=True)
    send_post = AsyncMock()
    monkeypatch.setattr(mod, 'send_post', send_post)
    with pytest.raises(HTTPException) as exc:
        await mod.create_channel_post_endpoint(_valid_request(), idempotency_key='k', db=AsyncMock(), admin=_admin())
    assert exc.value.status_code == 429
    send_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_idempotency_echo_returns_existing_without_send(monkeypatch):
    _patch_common(monkeypatch)
    existing = SimpleNamespace(
        id=99,
        channel_id='-1001',
        status='sent',
        telegram_message_id=1,
        message_thread_id=None,
        error_code=None,
        created_at=None,
    )
    monkeypatch.setattr(mod, 'get_channel_post_by_idempotency_key', AsyncMock(return_value=existing))
    send_post = AsyncMock()
    monkeypatch.setattr(mod, 'send_post', send_post)

    resp = await mod.create_channel_post_endpoint(
        _valid_request(), idempotency_key='dup', db=AsyncMock(), admin=_admin()
    )
    assert resp.id == 99
    send_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_capability_false_conflict(monkeypatch):
    _patch_common(monkeypatch, can_post=False)
    send_post = AsyncMock()
    monkeypatch.setattr(mod, 'send_post', send_post)
    with pytest.raises(HTTPException) as exc:
        await mod.create_channel_post_endpoint(_valid_request(), idempotency_key='k', db=AsyncMock(), admin=_admin())
    assert exc.value.status_code == 409
    send_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_empty_post_422(monkeypatch):
    _patch_common(monkeypatch)
    send_post = AsyncMock()
    monkeypatch.setattr(mod, 'send_post', send_post)
    req = ChannelPostRequest(destination_id='-1001')  # no text, no media
    with pytest.raises(HTTPException) as exc:
        await mod.create_channel_post_endpoint(req, idempotency_key='k', db=AsyncMock(), admin=_admin())
    assert exc.value.status_code == 422
    send_post.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_success_returns_row_and_audits(monkeypatch):
    log = _patch_common(monkeypatch)
    row = SimpleNamespace(
        id=5,
        channel_id='-1001',
        status='sent',
        telegram_message_id=555,
        message_thread_id=None,
        error_code=None,
        created_at=None,
    )
    send_post = AsyncMock(return_value=row)
    monkeypatch.setattr(mod, 'send_post', send_post)
    db = AsyncMock()

    resp = await mod.create_channel_post_endpoint(_valid_request(), idempotency_key='k', db=db, admin=_admin())

    assert resp.status == 'sent'
    assert resp.telegram_message_id == 555
    send_post.assert_awaited_once()
    log.assert_awaited_once()
    _, kwargs = log.await_args
    assert kwargs['action'] == 'channel_post'
    assert kwargs['resource_type'] == 'channel_post'
    assert kwargs['resource_id'] == '-1001'
    assert kwargs['status'] == 'success'
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_send_timeout_maps_to_gateway_timeout(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(mod, 'send_post', AsyncMock(side_effect=mod.ChannelPostSendError(mod.ERROR_TIMEOUT, 'timeout')))
    with pytest.raises(HTTPException) as exc:
        await mod.create_channel_post_endpoint(_valid_request(), idempotency_key='k', db=AsyncMock(), admin=_admin())
    assert exc.value.status_code == 504


# ── allowlist add / revoke / list ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_allowlist_add_creates_and_audits(monkeypatch):
    chat = SimpleNamespace(id=-1009, title='Grp', type='channel', username=None, permissions=None)
    monkeypatch.setattr(mod, 'fetch_chat', AsyncMock(return_value=chat))
    monkeypatch.setattr(
        mod,
        'resolve_capability',
        AsyncMock(return_value={'chat_id': '-1009', 'title': 'Grp', 'type': 'channel', 'can_post': True}),
    )
    upsert = AsyncMock()
    monkeypatch.setattr(mod, 'upsert_post_target', upsert)
    log = AsyncMock()
    monkeypatch.setattr(mod, 'PermissionService', SimpleNamespace(log_action=log))
    db = AsyncMock()

    resp = await mod.add_to_allowlist(mod.AllowlistAddRequest(input='@grp'), db=db, admin=_admin())

    assert resp.channel_id == '-1009'
    assert resp.title == 'Grp'
    upsert.assert_awaited_once()
    _, kwargs = upsert.await_args
    assert kwargs['channel_id'] == '-1009'
    log.assert_awaited_once()
    _, lkwargs = log.await_args
    assert lkwargs['action'] == 'channel_post_allowlist_add'
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_allowlist_add_not_postable_409(monkeypatch):
    chat = SimpleNamespace(id=-1009, title='Grp', type='channel', username=None, permissions=None)
    monkeypatch.setattr(mod, 'fetch_chat', AsyncMock(return_value=chat))
    monkeypatch.setattr(
        mod,
        'resolve_capability',
        AsyncMock(return_value={'chat_id': '-1009', 'title': 'Grp', 'type': 'channel', 'can_post': False}),
    )
    upsert = AsyncMock()
    monkeypatch.setattr(mod, 'upsert_post_target', upsert)
    monkeypatch.setattr(mod, 'PermissionService', SimpleNamespace(log_action=AsyncMock()))
    with pytest.raises(HTTPException) as exc:
        await mod.add_to_allowlist(mod.AllowlistAddRequest(input='@grp'), db=AsyncMock(), admin=_admin())
    assert exc.value.status_code == 409
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_allowlist_revoke_missing_404(monkeypatch):
    monkeypatch.setattr(mod, 'revoke_post_target', AsyncMock(return_value=False))
    monkeypatch.setattr(mod, 'PermissionService', SimpleNamespace(log_action=AsyncMock()))
    with pytest.raises(HTTPException) as exc:
        await mod.revoke_from_allowlist('-1009', db=AsyncMock(), admin=_admin())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_allowlist_revoke_success_audits(monkeypatch):
    monkeypatch.setattr(mod, 'revoke_post_target', AsyncMock(return_value=True))
    log = AsyncMock()
    monkeypatch.setattr(mod, 'PermissionService', SimpleNamespace(log_action=log))
    db = AsyncMock()
    result = await mod.revoke_from_allowlist('-1009', db=db, admin=_admin())
    assert result is None
    log.assert_awaited_once()
    _, kwargs = log.await_args
    assert kwargs['action'] == 'channel_post_allowlist_revoke'
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_allowlist_list_maps_targets(monkeypatch):
    targets = [
        SimpleNamespace(channel_id='-1001', title='A'),
        SimpleNamespace(channel_id='-1002', title=None),
    ]
    monkeypatch.setattr(mod, 'get_post_targets', AsyncMock(return_value=targets))
    resp = await mod.list_allowlist(db=AsyncMock(), _admin=_admin())
    assert [e.channel_id for e in resp] == ['-1001', '-1002']
    assert resp[0].title == 'A'
    assert resp[1].type is None


@pytest.mark.asyncio
async def test_history_lists_recent(monkeypatch):
    rows = [
        SimpleNamespace(
            id=2,
            channel_id='-1001',
            status='sent',
            telegram_message_id=9,
            message_thread_id=None,
            error_code=None,
            created_at=None,
        ),
    ]
    monkeypatch.setattr(mod, 'get_recent_channel_posts', AsyncMock(return_value=rows))
    resp = await mod.list_channel_posts(db=AsyncMock(), _admin=_admin())
    assert resp[0].id == 2
