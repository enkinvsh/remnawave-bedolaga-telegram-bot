"""Tests for GET /cabinet/admin/stats/campaigns/top (dashboard "Топ РК ссылок").

Locks the bug fix: the endpoint must rank the FULL campaign set (never the first
100 pre-sort) and apply ``limit`` only AFTER sorting, feed off the single aggregate
CRUD (no per-campaign N+1), expose the new starts fields, and keep every existing
field/semantic the current frontend relies on. Also covers zero-activity ranking,
summary totals over the full set, routing, and the unchanged ``stats:read`` RBAC.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes import admin_stats as m
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


def _patch_stats(monkeypatch, rows):
    captured = {}

    async def fake_stats(_db, campaign_ids=None):
        captured['ids'] = campaign_ids
        return list(rows)

    monkeypatch.setattr(m, 'get_campaigns_aggregate_stats', fake_stats)
    return captured


async def _call(limit=20):
    return await m.get_top_campaigns(limit=limit, admin=MagicMock(id=1), db=AsyncMock())


# ---- ranking ----


@pytest.mark.asyncio
async def test_ranks_three_campaigns_by_revenue_desc(monkeypatch):
    _patch_stats(
        monkeypatch,
        [
            _stats(campaign_id=1, name='low', total_amount_kopeks=10000, registrations=3),
            _stats(campaign_id=2, name='high', total_amount_kopeks=90000, registrations=5),
            _stats(campaign_id=3, name='mid', total_amount_kopeks=50000, registrations=4),
        ],
    )

    resp = await _call()

    assert [c.id for c in resp.campaigns] == [2, 3, 1]
    assert [c.total_revenue_kopeks for c in resp.campaigns] == [90000, 50000, 10000]


@pytest.mark.asyncio
async def test_secondary_sort_registrations_then_starts(monkeypatch):
    _patch_stats(
        monkeypatch,
        [
            _stats(campaign_id=1, name='A', total_amount_kopeks=1000, registrations=5, starts_total=10),
            _stats(campaign_id=2, name='B', total_amount_kopeks=1000, registrations=5, starts_total=50),
            _stats(campaign_id=3, name='C', total_amount_kopeks=1000, registrations=8, starts_total=1),
            _stats(campaign_id=4, name='D', total_amount_kopeks=5000, registrations=1, starts_total=1),
        ],
    )

    resp = await _call()

    assert [c.id for c in resp.campaigns] == [4, 3, 2, 1]


@pytest.mark.asyncio
async def test_requests_full_set_no_pre_sort_truncation(monkeypatch):
    captured = _patch_stats(monkeypatch, [_stats(campaign_id=1)])

    await _call(limit=5)

    assert captured['ids'] is None


# ---- limit applied AFTER sort ----


@pytest.mark.asyncio
async def test_limit_applied_after_sort_keeps_top_earner(monkeypatch):
    rows = [
        _stats(campaign_id=1, total_amount_kopeks=100, registrations=1),
        _stats(campaign_id=2, total_amount_kopeks=200, registrations=1),
        _stats(campaign_id=3, total_amount_kopeks=300, registrations=1),
        _stats(campaign_id=4, total_amount_kopeks=400, registrations=1),
        _stats(campaign_id=5, name='top', total_amount_kopeks=999000, registrations=9),
    ]
    _patch_stats(monkeypatch, rows)

    resp = await _call(limit=3)

    assert len(resp.campaigns) == 3
    assert resp.campaigns[0].id == 5
    assert 5 in [c.id for c in resp.campaigns]
    assert resp.total_campaigns == 5


# ---- zero-activity ranking + full-set totals ----


@pytest.mark.asyncio
async def test_zero_activity_campaigns_rank_last_totals_over_full_set(monkeypatch):
    _patch_stats(
        monkeypatch,
        [
            _stats(campaign_id=1, name='dead1', starts_total=0, registrations=0, total_amount_kopeks=0),
            _stats(
                campaign_id=2,
                name='earner',
                starts_total=40,
                starts_unique=30,
                registrations=7,
                paying_users=2,
                total_amount_kopeks=55912,
            ),
            _stats(campaign_id=3, name='dead2', starts_total=0, registrations=0, total_amount_kopeks=0),
            _stats(
                campaign_id=4,
                name='small',
                starts_total=5,
                starts_unique=4,
                registrations=2,
                paying_users=0,
                total_amount_kopeks=0,
            ),
        ],
    )

    resp = await _call()

    assert resp.campaigns[0].id == 2
    assert [c.id for c in resp.campaigns[-2:]] == [1, 3]
    assert resp.total_campaigns == 4
    assert resp.total_starts == 45
    assert resp.total_registrations == 9
    assert resp.total_revenue_kopeks == 55912


@pytest.mark.asyncio
async def test_empty_campaign_set_yields_zero_totals(monkeypatch):
    _patch_stats(monkeypatch, [])

    resp = await _call()

    assert resp.campaigns == []
    assert resp.total_campaigns == 0
    assert resp.total_starts == 0
    assert resp.total_registrations == 0
    assert resp.total_revenue_kopeks == 0


# ---- new fields + preserved semantics ----


@pytest.mark.asyncio
async def test_new_starts_fields_present_on_items_and_summary(monkeypatch):
    _patch_stats(
        monkeypatch,
        [_stats(campaign_id=1, starts_total=12, starts_unique=9, registrations=3)],
    )

    resp = await _call()

    item = resp.campaigns[0]
    assert item.starts_total == 12
    assert item.starts_unique == 9
    assert resp.total_starts == 12


@pytest.mark.asyncio
async def test_conversion_and_avg_derived_from_aggregate(monkeypatch):
    _patch_stats(
        monkeypatch,
        [_stats(campaign_id=1, registrations=4, paying_users=1, total_amount_kopeks=30000)],
    )

    resp = await _call()

    item = resp.campaigns[0]
    assert item.conversions == 1
    assert item.conversion_rate == 25.0
    assert item.total_revenue_kopeks == 30000
    assert item.avg_revenue_per_user_kopeks == 7500


@pytest.mark.asyncio
async def test_zero_registration_item_has_safe_conversion_and_avg(monkeypatch):
    _patch_stats(
        monkeypatch,
        [_stats(campaign_id=1, registrations=0, paying_users=0, total_amount_kopeks=0)],
    )

    resp = await _call()

    item = resp.campaigns[0]
    assert item.conversion_rate == 0.0
    assert item.avg_revenue_per_user_kopeks == 0


@pytest.mark.asyncio
async def test_item_preserves_existing_identity_fields(monkeypatch):
    from datetime import UTC, datetime

    created = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    _patch_stats(
        monkeypatch,
        [
            _stats(
                campaign_id=7,
                name='Gemini access',
                start_parameter='gem',
                bonus_type='subscription',
                is_active=False,
                created_at=created,
            )
        ],
    )

    resp = await _call()

    item = resp.campaigns[0]
    assert item.id == 7
    assert item.name == 'Gemini access'
    assert item.start_parameter == 'gem'
    assert item.bonus_type == 'subscription'
    assert item.is_active is False
    assert item.created_at == created.isoformat()


# ---- routing + RBAC (unchanged stats:read) ----


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


def test_top_campaigns_route_registered_under_cabinet_prefix():
    from app.cabinet.routes import router

    assert _find_get_route(router, '/cabinet/admin/stats/campaigns/top') is not None


def test_top_campaigns_uses_stats_read_like_other_stats_endpoints():
    from app.cabinet.routes import router

    top = _find_get_route(router, '/cabinet/admin/stats/campaigns/top')
    payments = _find_get_route(router, '/cabinet/admin/stats/payments/recent')
    assert _required_permissions(top) == {'stats:read'}
    assert _required_permissions(top) == _required_permissions(payments)


@pytest.mark.asyncio
async def test_top_campaigns_forbidden_for_non_admin(monkeypatch):
    from app.cabinet import dependencies as deps
    from app.cabinet.routes import router
    from app.services.permission_service import PermissionService

    guard = _require_permission_call(_find_get_route(router, '/cabinet/admin/stats/campaigns/top'))
    assert guard is not None

    monkeypatch.setattr(deps, 'get_client_ip', lambda _request: '127.0.0.1')
    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(False, 'denied')))
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())

    request = MagicMock()
    request.headers = {}

    with pytest.raises(HTTPException) as exc:
        await guard(request=request, user=MagicMock(id=1), db=AsyncMock())

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
