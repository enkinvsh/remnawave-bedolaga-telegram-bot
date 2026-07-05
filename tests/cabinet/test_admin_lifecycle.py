"""Tests for /cabinet/admin/notifications/lifecycle (GET list + PUT update).

Covers the shallow config validation (422), unknown-key guard (404), the merged
defaults+override GET shape with sent_count, the PUT upsert + cache-refresh flow,
and the RBAC wiring (settings:read on GET, settings:edit on PUT; non-admin -> 403).
Direct-call unit tests with a fake async session, per the cabinet test convention.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes import admin_lifecycle as m


def _rule(key: str, enabled: bool, config: dict):
    return types.SimpleNamespace(key=key, enabled=enabled, config=config)


# ── shallow config validation (pure) ──────────────────────────────────────


def test_validate_config_accepts_valid_scalars_and_nested():
    # Не должно бросить.
    m._validate_config(
        {
            'first_offset_hours': 1,
            'discount_percent': 100,
            'enabled_extra': True,
            'message_template': 'text {percent}',
            'steps': [{'offset_hours': 1, 'discount_percent': 0}],
            'message_templates': {'1': 'a', '24': 'b'},
        }
    )


def test_validate_config_rejects_negative_number():
    with pytest.raises(HTTPException) as exc:
        m._validate_config({'repeat_hours': -5})
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_validate_config_rejects_percent_over_100():
    with pytest.raises(HTTPException) as exc:
        m._validate_config({'discount_percent': 150})
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_validate_config_rejects_numeric_template():
    with pytest.raises(HTTPException) as exc:
        m._validate_config({'message_template': 5})
    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_validate_config_allows_percent_zero():
    m._validate_config({'discount_percent': 0})  # 0% — валидно


# ── GET list ───────────────────────────────────────────────────────────────


async def test_list_returns_all_rules_with_defaults(monkeypatch):
    monkeypatch.setattr(m, 'get_all_rules', AsyncMock(return_value=[]))
    monkeypatch.setattr(m, 'get_sent_counts', AsyncMock(return_value={}))

    views = await m.list_lifecycle_rules(admin=MagicMock(id=1), db=AsyncMock())

    assert len(views) == 8
    by_key = {v.key: v for v in views}
    assert by_key['expired_second_wave'].config == {'discount_percent': 10, 'valid_hours': 24}
    assert by_key['expired_second_wave'].enabled is True
    assert by_key['expired_second_wave'].sent_count == 0
    assert by_key['trial_not_activated'].group == 'pre_trial'


async def test_list_applies_db_override_and_sent_count(monkeypatch):
    monkeypatch.setattr(
        m,
        'get_all_rules',
        AsyncMock(return_value=[_rule('expired_second_wave', False, {'discount_percent': 15})]),
    )
    monkeypatch.setattr(m, 'get_sent_counts', AsyncMock(return_value={'expired_second_wave': 7}))

    views = await m.list_lifecycle_rules(admin=MagicMock(id=1), db=AsyncMock())

    second = next(v for v in views if v.key == 'expired_second_wave')
    assert second.enabled is False
    assert second.config == {'discount_percent': 15, 'valid_hours': 24}  # override + default merge
    assert second.sent_count == 7


# ── PUT update ─────────────────────────────────────────────────────────────


async def test_update_unknown_key_returns_404(monkeypatch):
    monkeypatch.setattr(m, 'upsert_rule', AsyncMock())
    payload = m.LifecycleRuleUpdate(enabled=True, config={})

    with pytest.raises(HTTPException) as exc:
        await m.update_lifecycle_rule(key='does_not_exist', payload=payload, admin=MagicMock(id=1), db=AsyncMock())

    assert exc.value.status_code == status.HTTP_404_NOT_FOUND


async def test_update_invalid_config_returns_422(monkeypatch):
    upsert = AsyncMock()
    monkeypatch.setattr(m, 'upsert_rule', upsert)
    payload = m.LifecycleRuleUpdate(enabled=True, config={'discount_percent': 200})

    with pytest.raises(HTTPException) as exc:
        await m.update_lifecycle_rule(key='expired_second_wave', payload=payload, admin=MagicMock(id=1), db=AsyncMock())

    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    upsert.assert_not_awaited()  # не пишем при провале валидации


async def test_update_valid_upserts_refreshes_cache_and_returns_view(monkeypatch):
    upsert = AsyncMock(return_value=_rule('expired_second_wave', True, {'discount_percent': 12, 'valid_hours': 24}))
    reload_mock = AsyncMock()
    monkeypatch.setattr(m, 'upsert_rule', upsert)
    monkeypatch.setattr(m.NotificationSettingsService, 'reload', reload_mock)
    monkeypatch.setattr(m, 'get_sent_counts', AsyncMock(return_value={'expired_second_wave': 3}))

    payload = m.LifecycleRuleUpdate(enabled=True, config={'discount_percent': 12, 'valid_hours': 24})
    view = await m.update_lifecycle_rule(
        key='expired_second_wave', payload=payload, admin=MagicMock(id=1, telegram_id=99), db=AsyncMock()
    )

    upsert.assert_awaited_once()
    reload_mock.assert_awaited_once()  # кэш сервиса обновлён после записи
    assert view.key == 'expired_second_wave'
    assert view.enabled is True
    assert view.config == {'discount_percent': 12, 'valid_hours': 24}
    assert view.sent_count == 3


# ── routing + RBAC ─────────────────────────────────────────────────────────


def _iter_dependants(dependant):
    yield dependant
    for sub in getattr(dependant, 'dependencies', []):
        yield from _iter_dependants(sub)


def _find_route(router, path, method):
    for route in router.routes:
        methods = getattr(route, 'methods', None) or set()
        if getattr(route, 'path', None) == path and method in methods:
            return route
    return None


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


def _require_permission_call(route):
    for dep in _iter_dependants(route.dependant):
        call = getattr(dep, 'call', None)
        if call is not None and getattr(call, '__qualname__', '').endswith('require_permission.<locals>.dependency'):
            return call
    return None


def test_routes_registered_under_cabinet_prefix():
    from app.cabinet.routes import router

    assert _find_route(router, '/cabinet/admin/notifications/lifecycle', 'GET') is not None
    assert _find_route(router, '/cabinet/admin/notifications/lifecycle/{key}', 'PUT') is not None


def test_get_uses_settings_read_and_put_uses_settings_edit():
    from app.cabinet.routes import router

    get_route = _find_route(router, '/cabinet/admin/notifications/lifecycle', 'GET')
    put_route = _find_route(router, '/cabinet/admin/notifications/lifecycle/{key}', 'PUT')
    assert _required_permissions(get_route) == {'settings:read'}
    assert _required_permissions(put_route) == {'settings:edit'}


async def test_put_forbidden_for_non_admin(monkeypatch):
    from app.cabinet import dependencies as deps
    from app.cabinet.routes import router
    from app.services.permission_service import PermissionService

    guard = _require_permission_call(_find_route(router, '/cabinet/admin/notifications/lifecycle/{key}', 'PUT'))
    assert guard is not None

    monkeypatch.setattr(deps, 'get_client_ip', lambda _request: '127.0.0.1')
    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(False, 'denied')))
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())

    request = MagicMock()
    request.headers = {}

    with pytest.raises(HTTPException) as exc:
        await guard(request=request, user=MagicMock(id=1), db=AsyncMock())

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
