"""Тесты пре-флайта рассылки (D2).

Инцидент: рассылка ушла на 283 получателя и дала Sent=0 / Errors=250 — всю пачку
сожгло системно битое сообщение (битый HTML / мёртвый media file_id). Пре-флайт
делает ОДНУ реальную тестовую отправку в служебный чат и падает типизированной
ошибкой ДО фан-аута. Здесь пинуется: тот же путь отправки, что и в боевой рассылке,
классификация сбоя в стабильный reason, best-effort удаление и уважение флага.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from fastapi import HTTPException

from app.services import broadcast_preflight as bp
from app.services.broadcast_preflight import BroadcastPreflightError, preflight_broadcast_message
from app.services.broadcast_service import BroadcastMediaConfig


_CHAT_ID = 424242


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=MagicMock(), message=message)


def _make_bot(*, send_error: BaseException | None = None, delete_error: BaseException | None = None):
    """Бот-заглушка: все send_* возвращают сообщение с message_id, либо кидают send_error."""
    bot = MagicMock()
    message = SimpleNamespace(message_id=4242)
    for name in ('send_message', 'send_photo', 'send_video', 'send_document'):
        mock = AsyncMock(side_effect=send_error) if send_error is not None else AsyncMock(return_value=message)
        setattr(bot, name, mock)
    bot.delete_message = AsyncMock(side_effect=delete_error)
    return bot, message


@pytest.fixture
def _chat(monkeypatch) -> None:
    """Резолвер служебного чата → фиксированный id, чтобы не зависеть от настроек окружения."""
    monkeypatch.setattr(bp, 'resolve_service_chat_id', lambda: _CHAT_ID)


# ============ Контракт reason-ключей ============


def test_reason_constants_are_stable() -> None:
    assert (bp.REASON_INVALID_HTML, bp.REASON_INVALID_MEDIA, bp.REASON_PREFLIGHT_FAILED) == (
        'invalid_html',
        'invalid_media',
        'preflight_failed',
    )


def test_error_carries_reason_and_detail() -> None:
    err = BroadcastPreflightError('invalid_html', 'boom')
    assert (err.reason_key, err.detail) == ('invalid_html', 'boom')
    assert 'invalid_html' in str(err) and 'boom' in str(err)


# ============ Флаг ============


@pytest.mark.asyncio
async def test_disabled_flag_skips_send(monkeypatch) -> None:
    monkeypatch.setattr(bp.settings, 'BROADCAST_PREFLIGHT_ENABLED', False)
    bot, _ = _make_bot()

    await preflight_broadcast_message(bot, message_text='hi')

    bot.send_message.assert_not_awaited()
    bot.delete_message.assert_not_awaited()


# ============ Happy path ============


@pytest.mark.asyncio
async def test_happy_text_send_uses_html_and_deletes(_chat) -> None:
    bot, message = _make_bot()
    keyboard = MagicMock()

    await preflight_broadcast_message(bot, message_text='<b>hi</b>', keyboard=keyboard)

    bot.send_message.assert_awaited_once()
    kwargs = bot.send_message.await_args.kwargs
    assert kwargs == {
        'chat_id': _CHAT_ID,
        'text': '<b>hi</b>',
        'parse_mode': 'HTML',
        'reply_markup': keyboard,
    }
    bot.send_photo.assert_not_awaited()
    bot.delete_message.assert_awaited_once_with(chat_id=_CHAT_ID, message_id=message.message_id)


@pytest.mark.asyncio
async def test_happy_media_send_and_deletes(_chat) -> None:
    bot, message = _make_bot()
    keyboard = MagicMock()
    media = BroadcastMediaConfig(type='photo', file_id='FID123', caption='подпись')

    await preflight_broadcast_message(bot, message_text='body', media=media, keyboard=keyboard)

    bot.send_photo.assert_awaited_once()
    kwargs = bot.send_photo.await_args.kwargs
    assert kwargs == {
        'chat_id': _CHAT_ID,
        'photo': 'FID123',
        'caption': 'подпись',
        'parse_mode': 'HTML',
        'reply_markup': keyboard,
    }
    bot.send_message.assert_not_awaited()
    bot.delete_message.assert_awaited_once_with(chat_id=_CHAT_ID, message_id=message.message_id)


@pytest.mark.asyncio
async def test_media_caption_falls_back_to_message_text(_chat) -> None:
    """caption = media.caption or message_text — та же композиция, что в _deliver_message."""
    bot, _ = _make_bot()
    media = BroadcastMediaConfig(type='photo', file_id='FID', caption=None)

    await preflight_broadcast_message(bot, message_text='fallback-text', media=media)

    assert bot.send_photo.await_args.kwargs['caption'] == 'fallback-text'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('media_type', 'method_name', 'kwarg'),
    [('photo', 'send_photo', 'photo'), ('video', 'send_video', 'video'), ('document', 'send_document', 'document')],
)
async def test_media_type_maps_to_correct_send_method(_chat, media_type, method_name, kwarg) -> None:
    bot, _ = _make_bot()
    media = BroadcastMediaConfig(type=media_type, file_id='FID', caption='c')

    await preflight_broadcast_message(bot, message_text='b', media=media)

    method = getattr(bot, method_name)
    method.assert_awaited_once()
    assert method.await_args.kwargs[kwarg] == 'FID'


# ============ Классификация сбоев ============


@pytest.mark.asyncio
async def test_parse_entities_error_maps_invalid_html(_chat) -> None:
    bot, _ = _make_bot(send_error=_bad_request("Bad Request: can't parse entities in message text"))

    with pytest.raises(BroadcastPreflightError) as exc:
        await preflight_broadcast_message(bot, message_text='<b>broken')

    assert exc.value.reason_key == bp.REASON_INVALID_HTML
    assert 'parse entities' in exc.value.detail
    bot.delete_message.assert_not_awaited()  # ничего не отправилось → нечего удалять


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'message',
    [
        'Bad Request: wrong file identifier/HTTP URL specified',
        'Bad Request: wrong file reference',
        'Bad Request: wrong remote file identifier specified',
    ],
)
async def test_dead_media_file_id_maps_invalid_media(_chat, message) -> None:
    bot, _ = _make_bot(send_error=_bad_request(message))
    media = BroadcastMediaConfig(type='photo', file_id='dead', caption='c')

    with pytest.raises(BroadcastPreflightError) as exc:
        await preflight_broadcast_message(bot, message_text='b', media=media)

    assert exc.value.reason_key == bp.REASON_INVALID_MEDIA


@pytest.mark.asyncio
async def test_other_bad_request_maps_preflight_failed(_chat) -> None:
    """Битый URL кнопки и прочие BadRequest, что не про HTML/медиа → preflight_failed."""
    bot, _ = _make_bot(send_error=_bad_request('Bad Request: BUTTON_URL_INVALID'))

    with pytest.raises(BroadcastPreflightError) as exc:
        await preflight_broadcast_message(bot, message_text='b', keyboard=MagicMock())

    assert exc.value.reason_key == bp.REASON_PREFLIGHT_FAILED
    assert exc.value.detail


@pytest.mark.asyncio
async def test_generic_exception_maps_preflight_failed(_chat) -> None:
    bot, _ = _make_bot(send_error=RuntimeError('unexpected network blip'))

    with pytest.raises(BroadcastPreflightError) as exc:
        await preflight_broadcast_message(bot, message_text='b')

    assert exc.value.reason_key == bp.REASON_PREFLIGHT_FAILED
    assert 'unexpected network blip' in exc.value.detail


@pytest.mark.asyncio
async def test_delete_failure_is_swallowed(_chat) -> None:
    """Отправка прошла → сообщение валидно. Провал удаления не должен ронять пре-флайт."""
    bot, _ = _make_bot(delete_error=RuntimeError('delete boom'))

    await preflight_broadcast_message(bot, message_text='hi')  # не бросает

    bot.delete_message.assert_awaited_once()


# ============ Резолвер служебного чата ============


def test_resolve_prefers_notification_channel(monkeypatch) -> None:
    monkeypatch.setattr(
        bp, 'settings', MagicMock(get_admin_notifications_chat_id=lambda: 555, get_admin_ids=lambda: [7, 8])
    )
    assert bp.resolve_service_chat_id() == 555


def test_resolve_falls_back_to_first_admin(monkeypatch) -> None:
    monkeypatch.setattr(
        bp, 'settings', MagicMock(get_admin_notifications_chat_id=lambda: None, get_admin_ids=lambda: [7, 8])
    )
    assert bp.resolve_service_chat_id() == 7


@pytest.mark.asyncio
async def test_no_service_chat_raises_clear_error(monkeypatch) -> None:
    monkeypatch.setattr(
        bp,
        'settings',
        MagicMock(
            BROADCAST_PREFLIGHT_ENABLED=True,
            get_admin_notifications_chat_id=lambda: None,
            get_admin_ids=list,
        ),
    )
    bot, _ = _make_bot()

    with pytest.raises(HTTPException) as exc:
        await preflight_broadcast_message(bot, message_text='hi')

    assert exc.value.status_code == 500
    assert 'No chat configured' in exc.value.detail
    bot.send_message.assert_not_awaited()


# ============ Юниты хелперов ============


def test_sanitize_detail_collapses_whitespace_and_caps_length() -> None:
    assert bp._sanitize_detail('a\n b\t  c') == 'a b c'
    assert len(bp._sanitize_detail('x' * 500)) == 200


@pytest.mark.parametrize(
    ('message', 'expected'),
    [
        ("Telegram says: can't parse entities", 'invalid_html'),
        ('Bad Request: wrong file identifier', 'invalid_media'),
        ('Bad Request: wrong remote file', 'invalid_media'),
        ('Bad Request: something else entirely', 'preflight_failed'),
    ],
)
def test_classify_preflight_bad_request(message, expected) -> None:
    result = bp._classify_preflight_bad_request(_bad_request(message))
    assert result.reason_key == expected
    assert result.detail
