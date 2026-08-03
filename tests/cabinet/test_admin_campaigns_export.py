"""Tests for GET /cabinet/admin/campaigns/export (campaign funnel CSV export).

Covers the pure CSV serializer (exact agreed columns, kopeks->rub, deep-link,
formula-injection sanitization, zero-activity rows), the id/search query handling,
the route-ordering guarantee (must resolve before /{campaign_id}), and the RBAC wiring
(campaigns:read like the other read endpoints; non-admin -> 403).
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes import admin_campaigns as m
from app.database.crud.campaign import CampaignAggregateStats


def _stats(**kwargs) -> CampaignAggregateStats:
    base = {
        'campaign_id': 1,
        'name': 'Promo',
        'start_parameter': 'promo',
        'bonus_type': 'none',
        'is_active': True,
        'created_at': None,
        'updated_at': None,
        'starts_total': 0,
        'starts_unique': 0,
        'registrations': 0,
        'trial_users': 0,
        'trial_activated': 0,
        'paying_users': 0,
        'total_amount_kopeks': 0,
        'paid_users': 0,
    }
    base.update(kwargs)
    return CampaignAggregateStats(**base)


def _parse_csv(content: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content)))


async def _read_body(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return ''.join(chunks)


def _fixed_link(monkeypatch) -> None:
    monkeypatch.setattr(m, 'get_campaign_deep_link', lambda p: f'https://t.me/bot?start={p}')


# ---- pure serializer ----


def test_csv_header_is_exact_agreed_column_set(monkeypatch):
    _fixed_link(monkeypatch)
    rows = _parse_csv(m._campaigns_to_csv([]))
    assert rows == [
        [
            'name',
            'start_parameter',
            'link',
            'bonus_type',
            'is_active',
            'starts_total',
            'starts_unique',
            'registrations',
            'trial_users',
            'trial_activated',
            'paying_users',
            'total_amount_rub',
            'created_at',
            'updated_at',
        ]
    ]


def test_csv_row_values_and_kopeks_to_rub(monkeypatch):
    _fixed_link(monkeypatch)
    created = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    updated = datetime(2026, 6, 2, 11, 30, tzinfo=UTC)
    row = _stats(
        name='Gemini',
        start_parameter='gem',
        bonus_type='subscription',
        is_active=True,
        created_at=created,
        updated_at=updated,
        starts_total=9,
        starts_unique=6,
        registrations=4,
        trial_users=3,
        trial_activated=2,
        paying_users=2,
        total_amount_kopeks=55912,
    )
    _header, data = _parse_csv(m._campaigns_to_csv([row]))
    assert data == [
        'Gemini',
        'gem',
        'https://t.me/bot?start=gem',
        'subscription',
        'True',
        '9',
        '6',
        '4',
        '3',
        '2',
        '2',
        '559.12',
        created.isoformat(),
        updated.isoformat(),
    ]


def test_csv_empty_campaign_serializes_zeros(monkeypatch):
    _fixed_link(monkeypatch)
    _header, data = _parse_csv(m._campaigns_to_csv([_stats(name='Empty', start_parameter='mt')]))
    assert data == [
        'Empty',
        'mt',
        'https://t.me/bot?start=mt',
        'none',
        'True',
        '0',
        '0',
        '0',
        '0',
        '0',
        '0',
        '0.00',
        '',
        '',
    ]


def test_csv_sanitizes_formula_injection(monkeypatch):
    _fixed_link(monkeypatch)
    _header, data = _parse_csv(m._campaigns_to_csv([_stats(name='=cmd()', start_parameter='x')]))
    assert data[0] == "'=cmd()"


# ---- id parsing ----


def test_parse_ids_none_or_blank_means_all():
    assert m._parse_export_campaign_ids(None) is None
    assert m._parse_export_campaign_ids('') is None
    assert m._parse_export_campaign_ids('   ') is None


def test_parse_ids_splits_ints():
    assert m._parse_export_campaign_ids('1,2,3') == [1, 2, 3]
    assert m._parse_export_campaign_ids(' 4 , 5 ') == [4, 5]


def test_parse_ids_rejects_non_int():
    with pytest.raises(HTTPException) as exc:
        m._parse_export_campaign_ids('1,abc')
    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST


# ---- endpoint ----


@pytest.mark.asyncio
async def test_export_all_passes_none_and_streams_csv(monkeypatch):
    _fixed_link(monkeypatch)
    captured = {}

    async def fake_stats(_db, campaign_ids=None):
        captured['ids'] = campaign_ids
        return [
            _stats(
                campaign_id=1,
                name='A',
                start_parameter='a',
                starts_total=5,
                registrations=2,
                paying_users=1,
                total_amount_kopeks=30000,
            ),
            _stats(campaign_id=2, name='B', start_parameter='b'),
        ]

    monkeypatch.setattr(m, 'get_campaigns_aggregate_stats', fake_stats)

    resp = await m.export_campaigns_csv(admin=MagicMock(id=1), db=AsyncMock(), ids=None, search=None)

    assert captured['ids'] is None
    assert resp.media_type == 'text/csv'
    assert resp.headers['content-disposition'].startswith('attachment; filename="campaigns_export_')
    rows = _parse_csv(await _read_body(resp))
    assert [r[0] for r in rows] == ['name', 'A', 'B']
    assert rows[1][11] == '300.00'
    assert rows[2][5:12] == ['0', '0', '0', '0', '0', '0', '0.00']


@pytest.mark.asyncio
async def test_export_by_ids_forwards_parsed_ids(monkeypatch):
    _fixed_link(monkeypatch)
    captured = {}

    async def fake_stats(_db, campaign_ids=None):
        captured['ids'] = campaign_ids
        return []

    monkeypatch.setattr(m, 'get_campaigns_aggregate_stats', fake_stats)

    await m.export_campaigns_csv(admin=MagicMock(id=1), db=AsyncMock(), ids='7,8', search=None)

    assert captured['ids'] == [7, 8]


@pytest.mark.asyncio
async def test_export_search_filters_name_and_start_parameter(monkeypatch):
    _fixed_link(monkeypatch)

    async def fake_stats(_db, campaign_ids=None):
        return [
            _stats(campaign_id=1, name='Gemini access', start_parameter='gem'),
            _stats(campaign_id=2, name='News digest', start_parameter='news'),
            _stats(campaign_id=3, name='Other', start_parameter='promo_gem'),
        ]

    monkeypatch.setattr(m, 'get_campaigns_aggregate_stats', fake_stats)

    resp = await m.export_campaigns_csv(admin=MagicMock(id=1), db=AsyncMock(), ids=None, search='GEM')
    rows = _parse_csv(await _read_body(resp))
    assert [r[0] for r in rows] == ['name', 'Gemini access', 'Other']


@pytest.mark.asyncio
async def test_export_invalid_ids_returns_400(monkeypatch):
    monkeypatch.setattr(m, 'get_campaigns_aggregate_stats', AsyncMock())
    with pytest.raises(HTTPException) as exc:
        await m.export_campaigns_csv(admin=MagicMock(id=1), db=AsyncMock(), ids='oops', search=None)
    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST


# ---- routing + RBAC ----


def _iter_dependants(dependant):
    yield dependant
    for sub in getattr(dependant, 'dependencies', []):
        yield from _iter_dependants(sub)


def _find_get_route(router, path):
    for route in router.routes:
        methods = getattr(route, 'methods', None) or set()
        if getattr(route, 'path', None) == path and 'GET' in methods:
            return route
    return None


def _route_index(router, path, method):
    for i, route in enumerate(router.routes):
        methods = getattr(route, 'methods', None) or set()
        if getattr(route, 'path', None) == path and method in methods:
            return i
    return -1


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


def test_export_route_registered_under_cabinet_prefix():
    from app.cabinet.routes import router

    assert _find_get_route(router, '/cabinet/admin/campaigns/export') is not None


def test_export_route_precedes_campaign_id_route():
    from app.cabinet.routes import router

    export_idx = _route_index(router, '/cabinet/admin/campaigns/export', 'GET')
    detail_idx = _route_index(router, '/cabinet/admin/campaigns/{campaign_id}', 'GET')
    assert export_idx != -1
    assert detail_idx != -1
    assert export_idx < detail_idx


def test_export_uses_campaigns_read_like_other_read_endpoints():
    from app.cabinet.routes import router

    export = _find_get_route(router, '/cabinet/admin/campaigns/export')
    list_route = _find_get_route(router, '/cabinet/admin/campaigns')
    assert _required_permissions(export) == {'campaigns:read'}
    assert _required_permissions(export) == _required_permissions(list_route)


@pytest.mark.asyncio
async def test_export_forbidden_for_non_admin(monkeypatch):
    from app.cabinet import dependencies as deps
    from app.cabinet.routes import router
    from app.services.permission_service import PermissionService

    guard = _require_permission_call(_find_get_route(router, '/cabinet/admin/campaigns/export'))
    assert guard is not None

    monkeypatch.setattr(deps, 'get_client_ip', lambda _request: '127.0.0.1')
    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(False, 'denied')))
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())

    request = MagicMock()
    request.headers = {}

    with pytest.raises(HTTPException) as exc:
        await guard(request=request, user=MagicMock(id=1), db=AsyncMock())

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
