"""Тесты диагностики провалов Telegram-рассылки (D1: error_summary).

Инцидент: реальная рассылка показала Total=283 / Sent=0 / Blocked=33 / Errors=250,
а у админа не было способа понять причину — per-user исключения уходили только в
логи сервера. Здесь пинуется стабильная классификация ошибок, агрегация
(counts + до 10 уникальных сэмплов) и её персист в broadcast_history.error_summary.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from app.services import broadcast_service as bs
from app.services.broadcast_service import BroadcastConfig, BroadcastService


def _bad_request(message: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=MagicMock(), message=message)


# ============ Классификация исключений → error_key ============


def test_retry_after_maps_to_retry_after_exhausted() -> None:
    result = bs._classify_send_exception(TelegramRetryAfter(method=MagicMock(), message='Flood', retry_after=5))
    assert result.status == 'failed'
    assert result.error_key == bs.ERR_RETRY_AFTER_EXHAUSTED
    assert result.error_text  # failed → текст сохраняется


def test_forbidden_maps_to_forbidden_blocked_without_text() -> None:
    result = bs._classify_send_exception(TelegramForbiddenError(method=MagicMock(), message='bot was blocked'))
    assert result.status == 'blocked'
    assert result.error_key == bs.ERR_FORBIDDEN_BLOCKED
    assert result.error_text is None  # blocked → текст не нужен


@pytest.mark.parametrize(
    'message',
    ['Bad Request: chat not found', 'Forbidden: bot was blocked by the user', 'Bad Request: user is deactivated'],
)
def test_bad_request_unreachable_recipient_maps_to_blocked(message: str) -> None:
    """'chat not found' / deactivated / blocked → blocked, НЕ failed (иначе 33 blocked стали бы errors)."""
    result = bs._classify_send_exception(_bad_request(message))
    assert result.status == 'blocked'
    assert result.error_key == bs.ERR_BAD_REQUEST_BLOCKED
    assert result.error_text is None


def test_bad_request_other_maps_to_bad_request_with_text() -> None:
    result = bs._classify_send_exception(_bad_request("Bad Request: can't parse entities"))
    assert result.status == 'failed'
    assert result.error_key == bs.ERR_BAD_REQUEST
    assert 'parse entities' in result.error_text


@pytest.mark.parametrize(
    'exc',
    [
        TelegramNetworkError(method=MagicMock(), message='read timeout'),
        TelegramServerError(method=MagicMock(), message='500 internal'),
    ],
)
def test_network_and_server_map_to_network_exhausted(exc: BaseException) -> None:
    result = bs._classify_send_exception(exc)
    assert result.status == 'failed'
    assert result.error_key == bs.ERR_NETWORK_EXHAUSTED
    assert result.error_text


def test_unknown_exception_maps_to_unexpected() -> None:
    result = bs._classify_send_exception(RuntimeError('something weird'))
    assert result.status == 'failed'
    assert result.error_key == bs.ERR_UNEXPECTED
    assert 'weird' in result.error_text


def test_error_key_constants_are_stable() -> None:
    """error_key попадает в БД и в кабинет — это контракт, ключи не переименовывать."""
    assert (
        bs.ERR_RETRY_AFTER_EXHAUSTED,
        bs.ERR_FORBIDDEN_BLOCKED,
        bs.ERR_BAD_REQUEST_BLOCKED,
        bs.ERR_BAD_REQUEST,
        bs.ERR_NETWORK_EXHAUSTED,
        bs.ERR_UNEXPECTED,
        bs.ERR_CANCELLED,
    ) == (
        'retry_after_exhausted',
        'forbidden_blocked',
        'bad_request_blocked',
        'bad_request',
        'network_exhausted',
        'unexpected',
        'cancelled',
    )


# ============ _sanitize_error ============


def test_sanitize_error_collapses_whitespace_and_caps_length() -> None:
    assert bs._sanitize_error('line1\nline2\t  x') == 'line1 line2 x'
    assert len(bs._sanitize_error('a' * 500)) == 200


# ============ _build_error_summary ============


def test_build_error_summary_returns_none_when_empty() -> None:
    assert bs._build_error_summary({}, []) is None


def test_build_error_summary_wraps_counts_and_samples() -> None:
    assert bs._build_error_summary({'bad_request': 2}, ['t']) == {'counts': {'bad_request': 2}, 'samples': ['t']}


# ============ Агрегация в _send_batched ============


def _service_with_delivery(plan: dict[int, BaseException | None]) -> BroadcastService:
    svc = BroadcastService()
    svc.set_bot(MagicMock())
    svc._update_progress = AsyncMock()

    async def deliver(telegram_id, config, keyboard):
        exc = plan.get(telegram_id)
        if exc is not None:
            raise exc

    svc._deliver_message = deliver
    return svc


@pytest.mark.asyncio
async def test_send_batched_aggregates_counts_and_dedupes_samples() -> None:
    import asyncio

    plan: dict[int, BaseException | None] = {
        100: _bad_request("Bad Request: can't parse entities"),
        101: _bad_request("Bad Request: can't parse entities"),
        102: _bad_request("Bad Request: can't parse entities"),
        200: TelegramForbiddenError(method=MagicMock(), message='blocked'),
        201: TelegramForbiddenError(method=MagicMock(), message='blocked'),
        300: _bad_request('Bad Request: chat not found'),
        400: None,
    }
    svc = _service_with_delivery(plan)
    config = BroadcastConfig(target='all', message_text='hi', selected_buttons=[])

    with patch('app.services.broadcast_service.asyncio.sleep', AsyncMock()):
        sent, failed, blocked, cancelled, summary = await svc._send_batched(
            1, list(plan.keys()), config, None, asyncio.Event()
        )

    assert (sent, failed, blocked, cancelled) == (1, 3, 3, False)
    assert summary['counts'] == {bs.ERR_BAD_REQUEST: 3, bs.ERR_FORBIDDEN_BLOCKED: 2, bs.ERR_BAD_REQUEST_BLOCKED: 1}
    # 3 одинаковых текста дедуплицируются в один сэмпл; blocked текста не дают
    assert len(summary['samples']) == 1
    assert "can't parse entities" in summary['samples'][0]


@pytest.mark.asyncio
async def test_send_batched_caps_samples_at_ten() -> None:
    import asyncio

    plan: dict[int, BaseException | None] = {
        tid: _bad_request(f'Bad Request: unique error {tid}') for tid in range(1, 16)
    }
    svc = _service_with_delivery(plan)
    config = BroadcastConfig(target='all', message_text='hi', selected_buttons=[])

    with patch('app.services.broadcast_service.asyncio.sleep', AsyncMock()):
        _sent, failed, _blocked, _cancelled, summary = await svc._send_batched(
            1, list(plan.keys()), config, None, asyncio.Event()
        )

    assert failed == 15
    assert summary['counts'][bs.ERR_BAD_REQUEST] == 15
    assert len(summary['samples']) == 10  # 15 уникальных текстов, но кап 10


@pytest.mark.asyncio
async def test_send_batched_mid_run_cancel_persists_summary_via_mark_cancelled() -> None:
    import asyncio

    svc = _service_with_delivery({})
    svc._mark_cancelled = AsyncMock()
    config = BroadcastConfig(target='all', message_text='hi', selected_buttons=[])

    cancel_event = asyncio.Event()
    cancel_event.set()  # отмена до старта батча

    with patch('app.services.broadcast_service.asyncio.sleep', AsyncMock()):
        sent, failed, blocked, cancelled, summary = await svc._send_batched(1, [1, 2, 3], config, None, cancel_event)

    assert cancelled is True
    assert summary is None  # ошибок ещё нет → None
    svc._mark_cancelled.assert_awaited_once_with(1, sent, failed, blocked, None)


# ============ Персист error_summary в broadcast_history ============


def _mock_session_with(broadcast: SimpleNamespace):
    mock_db = AsyncMock()
    mock_db.get = AsyncMock(return_value=broadcast)
    mock_db.commit = AsyncMock()
    session_local = MagicMock()
    session_local.return_value.__aenter__ = AsyncMock(return_value=mock_db)
    session_local.return_value.__aexit__ = AsyncMock(return_value=None)
    return session_local


@pytest.mark.asyncio
async def test_mark_finished_persists_error_summary() -> None:
    broadcast = SimpleNamespace(
        sent_count=0, failed_count=0, blocked_count=0, status='in_progress', error_summary=None, completed_at=None
    )
    summary = {'counts': {bs.ERR_BAD_REQUEST: 3}, 'samples': ['x']}

    with patch('app.services.broadcast_service.AsyncSessionLocal', _mock_session_with(broadcast)):
        await BroadcastService()._mark_finished(1, 5, 3, 2, cancelled=False, error_summary=summary)

    assert broadcast.error_summary == summary
    assert broadcast.status == 'partial'  # failed>0 → partial
    assert (broadcast.sent_count, broadcast.failed_count, broadcast.blocked_count) == (5, 3, 2)


@pytest.mark.asyncio
async def test_mark_finished_none_summary_does_not_overwrite() -> None:
    broadcast = SimpleNamespace(
        sent_count=0, failed_count=0, blocked_count=0, status='in_progress', error_summary='KEEP', completed_at=None
    )

    with patch('app.services.broadcast_service.AsyncSessionLocal', _mock_session_with(broadcast)):
        await BroadcastService()._mark_finished(1, 10, 0, 0, cancelled=False, error_summary=None)

    assert broadcast.error_summary == 'KEEP'  # None не затирает
    assert broadcast.status == 'completed'


# ============ API-ответ содержит error_summary ============


def test_serialize_broadcast_includes_error_summary() -> None:
    from app.cabinet.routes.admin_broadcasts import _serialize_broadcast

    summary = {'counts': {bs.ERR_BAD_REQUEST: 250}, 'samples': ["Bad Request: can't parse entities"]}
    broadcast = SimpleNamespace(
        id=1,
        target_type='all',
        message_text='hi',
        has_media=False,
        media_type=None,
        media_file_id=None,
        media_caption=None,
        total_count=283,
        sent_count=0,
        failed_count=250,
        blocked_count=33,
        status='partial',
        admin_id=1,
        admin_name='admin',
        created_at=datetime.now(UTC),
        completed_at=None,
        category='system',
        channel='telegram',
        email_subject=None,
        email_html_content=None,
        error_summary=summary,
    )

    response = _serialize_broadcast(broadcast)
    assert response.error_summary == summary
