"""Тесты интеграции пре-флайта в роуты создания рассылки (D2).

Контракт: оба эндпоинта создания (`POST ''` и `POST '/send'`) вызывают пре-флайт
ДО создания записи broadcast_history и запуска сервиса. Системно битое сообщение →
HTTP 422 с {'reason', 'detail'}, при этом запись НЕ создаётся и рассылка НЕ
стартует. Если инстанс бота недоступен — пре-флайт мягко пропускается.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes import admin_broadcasts as m
from app.cabinet.schemas.broadcasts import BroadcastCreateRequest, CombinedBroadcastCreateRequest
from app.services.broadcast_preflight import BroadcastPreflightError


_KB = object()  # сентинел клавиатуры, чтобы проверить проброс в пре-флайт


class _FakeResult:
    def all(self):
        return []


class _FakeSession:
    def __init__(self):
        self.added = []
        self.commit_count = 0
        self._next_id = 0

    async def execute(self, _stmt):
        return _FakeResult()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commit_count += 1

    async def refresh(self, obj):
        if getattr(obj, 'id', None) is None:
            self._next_id += 1
            obj.id = self._next_id


def _wire(monkeypatch, *, bot, preflight):
    """Общая проводка: инстанс бота, мок пре-флайта, заглушки старта и сериализации."""
    monkeypatch.setattr(m.broadcast_service, '_bot', bot)
    monkeypatch.setattr(m, 'preflight_broadcast_message', preflight)
    monkeypatch.setattr(m, 'create_broadcast_keyboard', MagicMock(return_value=_KB))
    monkeypatch.setattr(m.broadcast_service, 'start_broadcast', AsyncMock())
    monkeypatch.setattr(m.email_broadcast_service, 'start_broadcast', AsyncMock())
    monkeypatch.setattr(m, '_serialize_broadcast', MagicMock(return_value='OK'))


def _admin():
    return MagicMock(id=1, username='admin')


# ============ POST '' (create_broadcast) ============


@pytest.mark.asyncio
async def test_create_broadcast_preflight_failure_returns_422_no_row(monkeypatch) -> None:
    preflight = AsyncMock(side_effect=BroadcastPreflightError('invalid_html', 'bad html'))
    _wire(monkeypatch, bot=MagicMock(), preflight=preflight)
    db = _FakeSession()
    request = BroadcastCreateRequest(target='all', message_text='<b>broken', selected_buttons=[])

    with pytest.raises(HTTPException) as exc:
        await m.create_broadcast(request, _admin(), db)

    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert exc.value.detail == {'reason': 'invalid_html', 'detail': 'bad html'}
    assert db.added == []  # запись НЕ создана
    assert db.commit_count == 0
    m.broadcast_service.start_broadcast.assert_not_awaited()  # рассылка НЕ стартовала


@pytest.mark.asyncio
async def test_create_broadcast_preflight_pass_creates_row_and_starts(monkeypatch) -> None:
    preflight = AsyncMock()
    _wire(monkeypatch, bot=MagicMock(), preflight=preflight)
    db = _FakeSession()
    request = BroadcastCreateRequest(target='all', message_text='hello', selected_buttons=[])

    result = await m.create_broadcast(request, _admin(), db)

    assert result == 'OK'
    preflight.assert_awaited_once()
    assert preflight.await_args.kwargs['message_text'] == 'hello'
    assert preflight.await_args.kwargs['keyboard'] is _KB
    assert len(db.added) == 1
    assert db.commit_count == 1
    m.broadcast_service.start_broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_broadcast_bot_unavailable_skips_preflight(monkeypatch) -> None:
    preflight = AsyncMock()
    _wire(monkeypatch, bot=None, preflight=preflight)
    db = _FakeSession()
    request = BroadcastCreateRequest(target='all', message_text='hello', selected_buttons=[])

    result = await m.create_broadcast(request, _admin(), db)

    assert result == 'OK'
    preflight.assert_not_awaited()  # бота нет → пре-флайт пропущен
    assert len(db.added) == 1
    m.broadcast_service.start_broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_broadcast_forwards_media_to_preflight(monkeypatch) -> None:
    preflight = AsyncMock()
    _wire(monkeypatch, bot=MagicMock(), preflight=preflight)
    db = _FakeSession()
    request = BroadcastCreateRequest(
        target='all',
        message_text='cap-fallback',
        selected_buttons=[],
        media={'type': 'photo', 'file_id': 'FID', 'caption': None},
    )

    await m.create_broadcast(request, _admin(), db)

    media = preflight.await_args.kwargs['media']
    assert media.type == 'photo'
    assert media.file_id == 'FID'
    assert media.caption == 'cap-fallback'  # caption = media.caption or message_text


# ============ POST '/send' (create_combined_broadcast) ============


@pytest.mark.asyncio
async def test_combined_telegram_preflight_failure_returns_422_no_row(monkeypatch) -> None:
    preflight = AsyncMock(side_effect=BroadcastPreflightError('invalid_media', 'dead file'))
    _wire(monkeypatch, bot=MagicMock(), preflight=preflight)
    db = _FakeSession()
    request = CombinedBroadcastCreateRequest(channel='telegram', target='all', message_text='<b>x', selected_buttons=[])

    with pytest.raises(HTTPException) as exc:
        await m.create_combined_broadcast(request, _admin(), db)

    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert exc.value.detail == {'reason': 'invalid_media', 'detail': 'dead file'}
    assert db.added == []
    assert db.commit_count == 0
    m.broadcast_service.start_broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_combined_telegram_preflight_pass_creates_and_starts(monkeypatch) -> None:
    preflight = AsyncMock()
    _wire(monkeypatch, bot=MagicMock(), preflight=preflight)
    db = _FakeSession()
    request = CombinedBroadcastCreateRequest(
        channel='telegram', target='all', message_text='hi there', selected_buttons=[]
    )

    result = await m.create_combined_broadcast(request, _admin(), db)

    assert result == 'OK'
    preflight.assert_awaited_once()
    assert preflight.await_args.kwargs['message_text'] == 'hi there'
    assert len(db.added) == 1
    m.broadcast_service.start_broadcast.assert_awaited_once()


@pytest.mark.asyncio
async def test_combined_email_channel_skips_preflight(monkeypatch) -> None:
    preflight = AsyncMock()
    _wire(monkeypatch, bot=MagicMock(), preflight=preflight)
    db = _FakeSession()
    request = CombinedBroadcastCreateRequest(
        channel='email',
        target='all_email',
        email_subject='Subject',
        email_html_content='<p>body</p>',
    )

    result = await m.create_combined_broadcast(request, _admin(), db)

    assert result == 'OK'
    preflight.assert_not_awaited()  # email-канал не шлёт в telegram → пре-флайт не нужен
    assert len(db.added) == 1
    m.email_broadcast_service.start_broadcast.assert_awaited_once()
    m.broadcast_service.start_broadcast.assert_not_awaited()


# ============ Регистрация роутов и RBAC не сломаны ============


def _find_post_route(path):
    from app.cabinet.routes import router

    for route in router.routes:
        if getattr(route, 'path', None) == path and 'POST' in (getattr(route, 'methods', None) or set()):
            return route
    return None


def _iter_dependants(dependant):
    yield dependant
    for sub in getattr(dependant, 'dependencies', []):
        yield from _iter_dependants(sub)


def _required_permissions(route):
    perms: set[str] = set()
    for dep in _iter_dependants(route.dependant):
        call = getattr(dep, 'call', None)
        if call is None or not getattr(call, '__qualname__', '').endswith('require_permission.<locals>.dependency'):
            continue
        freevars = call.__code__.co_freevars
        if 'permissions' in freevars and call.__closure__:
            cell = call.__closure__[freevars.index('permissions')]
            perms.update(cell.cell_contents)
    return perms


def test_both_creation_routes_registered_with_rbac() -> None:
    create = _find_post_route('/cabinet/admin/broadcasts')
    send = _find_post_route('/cabinet/admin/broadcasts/send')

    assert create is not None
    assert send is not None
    assert 'broadcasts:create' in _required_permissions(create)
    assert 'broadcasts:send' in _required_permissions(send)
