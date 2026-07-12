"""channel_post_service — capability mapping, at-most-once send, isolation.

Service layer tests. The bot is injected via ``broadcast_service.set_bot`` (the
ONLY dependency taken from broadcast_service). CRUD is monkeypatched so no DB is
required. All chat ids are synthetic (multi-service invariant).
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from app.cabinet.schemas.channel_posts import ChannelPostButton, ChannelPostMedia
from app.services import channel_post_service as svc
from app.services.broadcast_service import broadcast_service


# ── helpers ────────────────────────────────────────────────────────────────


def _fake_bot(**overrides):
    bot = SimpleNamespace(
        id=42,
        get_chat_member=AsyncMock(),
        get_chat=AsyncMock(),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=555)),
        send_photo=AsyncMock(return_value=SimpleNamespace(message_id=556)),
        send_video=AsyncMock(return_value=SimpleNamespace(message_id=557)),
        send_document=AsyncMock(return_value=SimpleNamespace(message_id=558)),
    )
    for key, value in overrides.items():
        setattr(bot, key, value)
    return bot


class _Post(SimpleNamespace):
    pass


def _patch_crud(monkeypatch):
    """Wire in-memory CRUD doubles; return the created post + call counters."""
    created: dict = {}
    calls = SimpleNamespace(create=0, sent=0, failed=0)

    async def _create(db, **kwargs):
        calls.create += 1
        post = _Post(
            id=1,
            status='sending',
            telegram_message_id=None,
            error_code=None,
            **kwargs,
        )
        created['post'] = post
        return post

    async def _mark_sent(db, post, telegram_message_id):
        calls.sent += 1
        post.status = 'sent'
        post.telegram_message_id = telegram_message_id
        return post

    async def _mark_failed(db, post, error_code):
        calls.failed += 1
        post.status = 'failed'
        post.error_code = error_code
        return post

    monkeypatch.setattr(svc, 'create_channel_post', _create)
    monkeypatch.setattr(svc, 'mark_channel_post_sent', _mark_sent)
    monkeypatch.setattr(svc, 'mark_channel_post_failed', _mark_failed)
    return created, calls


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SimpleNamespace(), message=message)


# ── resolve_capability matrix ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_channel_admin_with_post_right_can_post(monkeypatch):
    bot = _fake_bot()
    bot.get_chat_member.return_value = SimpleNamespace(status='administrator', can_post_messages=True)
    bot.get_chat.return_value = SimpleNamespace(type='channel', title='C', username='c', permissions=None)
    broadcast_service.set_bot(bot)

    cap = await svc.resolve_capability(-1001, is_allowlisted=True)

    assert cap['can_post'] is True
    assert cap['type'] == 'channel'
    assert cap['chat_id'] == '-1001'
    assert cap['is_allowlisted'] is True


@pytest.mark.asyncio
async def test_channel_admin_without_post_right_cannot_post(monkeypatch):
    bot = _fake_bot()
    bot.get_chat_member.return_value = SimpleNamespace(status='administrator', can_post_messages=False)
    bot.get_chat.return_value = SimpleNamespace(type='channel', title='C', username=None, permissions=None)
    broadcast_service.set_bot(bot)

    cap = await svc.resolve_capability(-1002, is_allowlisted=True)
    assert cap['can_post'] is False


@pytest.mark.asyncio
async def test_supergroup_admin_can_post(monkeypatch):
    bot = _fake_bot()
    bot.get_chat_member.return_value = SimpleNamespace(status='administrator')
    bot.get_chat.return_value = SimpleNamespace(type='supergroup', title='G', username=None, permissions=None)
    broadcast_service.set_bot(bot)

    cap = await svc.resolve_capability(-1003, is_allowlisted=True)
    assert cap['can_post'] is True


@pytest.mark.asyncio
async def test_group_plain_member_unrestricted_with_chat_perms_can_post(monkeypatch):
    bot = _fake_bot()
    bot.get_chat_member.return_value = SimpleNamespace(status='member')
    bot.get_chat.return_value = SimpleNamespace(
        type='group',
        title='G',
        username=None,
        permissions=SimpleNamespace(can_send_messages=True),
    )
    broadcast_service.set_bot(bot)

    cap = await svc.resolve_capability(-1004, is_allowlisted=True)
    assert cap['can_post'] is True


@pytest.mark.asyncio
async def test_group_plain_member_without_chat_perms_cannot_post(monkeypatch):
    bot = _fake_bot()
    bot.get_chat_member.return_value = SimpleNamespace(status='member')
    bot.get_chat.return_value = SimpleNamespace(
        type='group',
        title='G',
        username=None,
        permissions=SimpleNamespace(can_send_messages=False),
    )
    broadcast_service.set_bot(bot)

    cap = await svc.resolve_capability(-1005, is_allowlisted=True)
    assert cap['can_post'] is False


@pytest.mark.asyncio
async def test_restricted_member_uses_own_can_send_messages(monkeypatch):
    bot = _fake_bot()
    bot.get_chat_member.return_value = SimpleNamespace(status='restricted', can_send_messages=False)
    bot.get_chat.return_value = SimpleNamespace(
        type='supergroup',
        title='G',
        username=None,
        permissions=SimpleNamespace(can_send_messages=True),  # baseline ignored for restricted
    )
    broadcast_service.set_bot(bot)

    cap = await svc.resolve_capability(-1006, is_allowlisted=True)
    assert cap['can_post'] is False


@pytest.mark.asyncio
async def test_left_and_kicked_cannot_post(monkeypatch):
    bot = _fake_bot()
    bot.get_chat.return_value = SimpleNamespace(type='supergroup', title='G', username=None, permissions=None)
    broadcast_service.set_bot(bot)

    for bad_status in ('left', 'kicked'):
        bot.get_chat_member.return_value = SimpleNamespace(status=bad_status)
        cap = await svc.resolve_capability(-1007, is_allowlisted=True)
        assert cap['can_post'] is False, bad_status


# ── send_post: at-most-once, one send, error mapping ────────────────────────


@pytest.mark.asyncio
async def test_send_text_only_calls_send_message_once(monkeypatch):
    created, calls = _patch_crud(monkeypatch)
    bot = _fake_bot()
    broadcast_service.set_bot(bot)

    post = await svc.send_post(
        db=AsyncMock(),
        chat_id_int=-1001,
        canonical_channel_id='-1001',
        title='T',
        message_text='hello',
        buttons=[ChannelPostButton(label='A', url='https://a.io')],
        media=None,
        keyboard=None,
        disable_web_page_preview=True,
        idempotency_key='k1',
        admin_id=7,
    )

    assert post.status == 'sent'
    assert post.telegram_message_id == 555
    assert bot.send_message.await_count == 1
    assert bot.send_photo.await_count == 0
    args, kwargs = bot.send_message.await_args
    assert args[0] == -1001  # int chat id
    assert kwargs['parse_mode'] == 'HTML'
    assert kwargs['disable_web_page_preview'] is True
    assert 'reply_markup' in kwargs
    # buttons_json persisted
    assert created['post'].buttons_json == [{'label': 'A', 'url': 'https://a.io'}]


@pytest.mark.asyncio
async def test_send_photo_calls_send_photo_with_caption(monkeypatch):
    created, calls = _patch_crud(monkeypatch)
    bot = _fake_bot()
    broadcast_service.set_bot(bot)

    media = ChannelPostMedia(type='photo', file_id='FILE')
    post = await svc.send_post(
        db=AsyncMock(),
        chat_id_int=-1002,
        canonical_channel_id='-1002',
        title=None,
        message_text='cap',
        buttons=[],
        media=media,
        keyboard=None,
        disable_web_page_preview=True,
        idempotency_key='k2',
        admin_id=7,
    )

    assert post.status == 'sent'
    assert bot.send_photo.await_count == 1
    assert bot.send_message.await_count == 0
    args, kwargs = bot.send_photo.await_args
    assert args[0] == -1002
    assert kwargs['caption'] == 'cap'
    assert kwargs['parse_mode'] == 'HTML'
    assert 'disable_web_page_preview' not in kwargs
    assert created['post'].media_json == {'type': 'photo', 'file_id': 'FILE'}


@pytest.mark.asyncio
async def test_send_video_and_document_dispatch(monkeypatch):
    for mtype, attr, mid in (('video', 'send_video', 557), ('document', 'send_document', 558)):
        created, calls = _patch_crud(monkeypatch)
        bot = _fake_bot()
        broadcast_service.set_bot(bot)
        post = await svc.send_post(
            db=AsyncMock(),
            chat_id_int=-1003,
            canonical_channel_id='-1003',
            title=None,
            message_text=None,
            buttons=[],
            media=ChannelPostMedia(type=mtype, file_id='F'),
            keyboard=None,
            disable_web_page_preview=True,
            idempotency_key=f'k-{mtype}',
            admin_id=7,
        )
        assert post.telegram_message_id == mid
        assert getattr(bot, attr).await_count == 1


@pytest.mark.asyncio
async def test_forbidden_marks_failed(monkeypatch):
    created, calls = _patch_crud(monkeypatch)
    bot = _fake_bot(
        send_message=AsyncMock(side_effect=TelegramForbiddenError(method=SimpleNamespace(), message='blocked'))
    )
    broadcast_service.set_bot(bot)

    post = await svc.send_post(
        db=AsyncMock(),
        chat_id_int=-1001,
        canonical_channel_id='-1001',
        title=None,
        message_text='x',
        buttons=[],
        media=None,
        keyboard=None,
        disable_web_page_preview=True,
        idempotency_key='kf',
        admin_id=7,
    )
    assert post.status == 'failed'
    assert post.error_code == svc.ERROR_FORBIDDEN
    assert calls.sent == 0


@pytest.mark.asyncio
async def test_bad_request_chat_not_found_code(monkeypatch):
    created, calls = _patch_crud(monkeypatch)
    bot = _fake_bot(send_message=AsyncMock(side_effect=_bad_request('chat not found')))
    broadcast_service.set_bot(bot)

    post = await svc.send_post(
        db=AsyncMock(),
        chat_id_int=-1,
        canonical_channel_id='-1',
        title=None,
        message_text='x',
        buttons=[],
        media=None,
        keyboard=None,
        disable_web_page_preview=True,
        idempotency_key='kb',
        admin_id=7,
    )
    assert post.status == 'failed'
    assert post.error_code == svc.ERROR_CHAT_NOT_FOUND


@pytest.mark.asyncio
async def test_retry_after_marks_failed(monkeypatch):
    created, calls = _patch_crud(monkeypatch)
    exc = TelegramRetryAfter(method=SimpleNamespace(), message='flood', retry_after=5)
    bot = _fake_bot(send_message=AsyncMock(side_effect=exc))
    broadcast_service.set_bot(bot)

    post = await svc.send_post(
        db=AsyncMock(),
        chat_id_int=-1001,
        canonical_channel_id='-1001',
        title=None,
        message_text='x',
        buttons=[],
        media=None,
        keyboard=None,
        disable_web_page_preview=True,
        idempotency_key='kr',
        admin_id=7,
    )
    assert post.status == 'failed'
    assert post.error_code == svc.ERROR_RETRY_AFTER


@pytest.mark.asyncio
async def test_timeout_leaves_sending_and_reraises(monkeypatch):
    created, calls = _patch_crud(monkeypatch)
    bot = _fake_bot(send_message=AsyncMock(side_effect=TimeoutError()))
    broadcast_service.set_bot(bot)

    with pytest.raises(svc.ChannelPostSendError) as exc:
        await svc.send_post(
            db=AsyncMock(),
            chat_id_int=-1001,
            canonical_channel_id='-1001',
            title=None,
            message_text='x',
            buttons=[],
            media=None,
            keyboard=None,
            disable_web_page_preview=True,
            idempotency_key='kt',
            admin_id=7,
        )
    assert exc.value.code == svc.ERROR_TIMEOUT
    # row left 'sending' — no mark_sent / mark_failed, NO retry
    assert created['post'].status == 'sending'
    assert calls.sent == 0
    assert calls.failed == 0
    assert bot.send_message.await_count == 1


# ── isolation invariants ────────────────────────────────────────────────────


def test_service_imports_only_bot_from_broadcast_service():
    source = Path(svc.__file__).read_text(encoding='utf-8')
    bs_imports = re.findall(r'^from app\.services\.broadcast_service import (.+)$', source, re.MULTILINE)
    assert bs_imports == ['broadcast_service']
    # No use of the broadcast history / recipient machinery.
    assert 'broadcast_history' not in source
    assert 'BroadcastService' not in source
    assert 'BroadcastConfig' not in source
