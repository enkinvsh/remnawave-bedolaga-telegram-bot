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


# ── forum message_thread_id passthrough ─────────────────────────────────────


async def _send_with_thread(monkeypatch, *, media, key):
    _patch_crud(monkeypatch)
    bot = _fake_bot()
    broadcast_service.set_bot(bot)
    await svc.send_post(
        db=AsyncMock(),
        chat_id_int=-1001,
        canonical_channel_id='-1001',
        title=None,
        message_text='hi',
        buttons=[],
        media=media,
        keyboard=None,
        disable_web_page_preview=True,
        idempotency_key=key,
        admin_id=7,
        message_thread_id=42,
    )
    return bot


@pytest.mark.asyncio
async def test_send_message_receives_thread_id(monkeypatch):
    bot = await _send_with_thread(monkeypatch, media=None, key='t-msg')
    _, kwargs = bot.send_message.await_args
    assert kwargs['message_thread_id'] == 42


@pytest.mark.asyncio
async def test_send_media_receives_thread_id(monkeypatch):
    for mtype, attr in (('photo', 'send_photo'), ('video', 'send_video'), ('document', 'send_document')):
        bot = await _send_with_thread(monkeypatch, media=ChannelPostMedia(type=mtype, file_id='F'), key=f't-{mtype}')
        _, kwargs = getattr(bot, attr).await_args
        assert kwargs['message_thread_id'] == 42


@pytest.mark.asyncio
async def test_thread_id_persisted_on_history_row(monkeypatch):
    created, _ = _patch_crud(monkeypatch)
    broadcast_service.set_bot(_fake_bot())
    await svc.send_post(
        db=AsyncMock(),
        chat_id_int=-1001,
        canonical_channel_id='-1001',
        title=None,
        message_text='hi',
        buttons=[],
        media=None,
        keyboard=None,
        disable_web_page_preview=True,
        idempotency_key='t-persist',
        admin_id=7,
        message_thread_id=7,
    )
    assert created['post'].message_thread_id == 7


@pytest.mark.asyncio
async def test_thread_id_defaults_none(monkeypatch):
    created, _ = _patch_crud(monkeypatch)
    bot = _fake_bot()
    broadcast_service.set_bot(bot)
    await svc.send_post(
        db=AsyncMock(),
        chat_id_int=-1001,
        canonical_channel_id='-1001',
        title=None,
        message_text='hi',
        buttons=[],
        media=None,
        keyboard=None,
        disable_web_page_preview=True,
        idempotency_key='t-none',
        admin_id=7,
    )
    _, kwargs = bot.send_message.await_args
    assert kwargs['message_thread_id'] is None
    assert created['post'].message_thread_id is None


# ── _classify_bad_request: honest forum-topic classification ─────────────────


def test_classify_topic_closed():
    assert svc._classify_bad_request(_bad_request('Bad Request: TOPIC_CLOSED')) == svc.ERROR_TOPIC_CLOSED


def test_classify_thread_not_found():
    assert (
        svc._classify_bad_request(_bad_request('Bad Request: message thread not found')) == svc.ERROR_THREAD_NOT_FOUND
    )


def test_classify_thread_not_found_not_stale_file():
    # 'message thread not found' contains 'not found' but NO 'file' → must not
    # collapse to stale_file_id.
    assert svc._classify_bad_request(_bad_request('Bad Request: message thread not found')) != svc.ERROR_STALE_FILE_ID


def test_classify_existing_codes_unchanged():
    assert svc._classify_bad_request(_bad_request('Bad Request: chat not found')) == svc.ERROR_CHAT_NOT_FOUND
    assert svc._classify_bad_request(_bad_request('Bad Request: file reference expired')) == svc.ERROR_STALE_FILE_ID
    assert svc._classify_bad_request(_bad_request("Bad Request: can't parse entities")) == svc.ERROR_INVALID_HTML
    assert svc._classify_bad_request(_bad_request('Bad Request: something else')) == svc.ERROR_BAD_REQUEST


# ── rich send path (sendRichMessage) ────────────────────────────────────────


def _rich_bot(*, message_id=999, side_effect=None):
    bot = AsyncMock()
    if side_effect is not None:
        bot.side_effect = side_effect
    else:
        bot.return_value = SimpleNamespace(message_id=message_id)
    return bot


async def _send_rich(monkeypatch, bot, *, key, keyboard=None, thread=None):
    _patch_crud(monkeypatch)
    broadcast_service.set_bot(bot)
    return await svc.send_post(
        db=AsyncMock(),
        chat_id_int=-1001,
        canonical_channel_id='-1001',
        title=None,
        message_text='<b>hi</b>',
        buttons=[],
        media=None,
        keyboard=keyboard,
        disable_web_page_preview=True,
        idempotency_key=key,
        admin_id=7,
        message_thread_id=thread,
        is_rich=True,
    )


@pytest.mark.asyncio
async def test_rich_send_makes_single_bot_call(monkeypatch):
    bot = _rich_bot()
    post = await _send_rich(monkeypatch, bot, key='r1', thread=5)
    assert post.status == 'sent'
    assert post.telegram_message_id == 999
    assert bot.await_count == 1
    method = bot.await_args.args[0]
    assert isinstance(method, svc.SendRichMessage)
    assert method.chat_id == -1001
    assert method.message_thread_id == 5
    assert method.rich_message == {'html': '<b>hi</b>', 'skip_entity_detection': True}


@pytest.mark.asyncio
async def test_rich_send_does_not_call_send_message(monkeypatch):
    bot = _rich_bot()
    bot.send_message = AsyncMock()
    await _send_rich(monkeypatch, bot, key='r2')
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_rich_send_passes_keyboard(monkeypatch):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='x', url='https://a.io')]])
    bot = _rich_bot()
    await _send_rich(monkeypatch, bot, key='r3', keyboard=kb)
    method = bot.await_args.args[0]
    assert method.reply_markup is kb


@pytest.mark.asyncio
async def test_rich_persisted_on_history_row(monkeypatch):
    created, _ = _patch_crud(monkeypatch)
    broadcast_service.set_bot(_rich_bot())
    await svc.send_post(
        db=AsyncMock(),
        chat_id_int=-1001,
        canonical_channel_id='-1001',
        title=None,
        message_text='<b>x</b>',
        buttons=[],
        media=None,
        keyboard=None,
        disable_web_page_preview=True,
        idempotency_key='r-persist',
        admin_id=7,
        is_rich=True,
    )
    assert created['post'].is_rich is True


@pytest.mark.asyncio
async def test_rich_bad_request_unsupported_marks_rich_not_supported(monkeypatch):
    exc = TelegramBadRequest(method=SimpleNamespace(), message='Bad Request: method sendRichMessage not supported')
    bot = _rich_bot(side_effect=exc)
    post = await _send_rich(monkeypatch, bot, key='r-unsup')
    assert post.status == 'failed'
    assert post.error_code == svc.ERROR_RICH_NOT_SUPPORTED


@pytest.mark.asyncio
async def test_rich_bad_request_other_uses_regular_classification(monkeypatch):
    exc = TelegramBadRequest(method=SimpleNamespace(), message='Bad Request: chat not found')
    bot = _rich_bot(side_effect=exc)
    post = await _send_rich(monkeypatch, bot, key='r-chat')
    assert post.status == 'failed'
    assert post.error_code == svc.ERROR_CHAT_NOT_FOUND


@pytest.mark.asyncio
async def test_rich_timeout_leaves_sending(monkeypatch):
    created, calls = _patch_crud(monkeypatch)
    bot = _rich_bot(side_effect=TimeoutError())
    broadcast_service.set_bot(bot)
    with pytest.raises(svc.ChannelPostSendError) as exc:
        await svc.send_post(
            db=AsyncMock(),
            chat_id_int=-1001,
            canonical_channel_id='-1001',
            title=None,
            message_text='<b>x</b>',
            buttons=[],
            media=None,
            keyboard=None,
            disable_web_page_preview=True,
            idempotency_key='r-timeout',
            admin_id=7,
            is_rich=True,
        )
    assert exc.value.code == svc.ERROR_TIMEOUT
    assert created['post'].status == 'sending'
    assert calls.sent == 0
    assert calls.failed == 0
