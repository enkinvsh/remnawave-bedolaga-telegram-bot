"""Tests for POST /cabinet/admin/campaigns/bulk-delete (mass delete + dry-run preview).

Covers the grouped child-row counter (single query, empty short-circuit), the route's
dry-run vs real-delete behaviour (dry-run counts and deletes nothing; real delete cascades
in one transaction and reports `deleted_count`), mixed existing/missing ids, id de-dup,
the pydantic size caps (empty -> 422, >500 -> 422), route ordering (before /{campaign_id}),
and the RBAC wiring (same `campaigns:delete` guard as single-delete; non-admin -> 403).

Mirrors the mocked-session / closure-introspection conventions of the sibling
test_admin_campaigns_bulk.py and test_admin_campaigns_export.py (no real DB in this suite).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import Delete

from app.cabinet.routes import admin_campaigns as m
from app.cabinet.schemas.campaigns import BulkCampaignDeleteRequest
from app.database.models import AdvertisingCampaignRegistration, AdvertisingCampaignStart


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Answers the resolve-existing SELECT (call #1) and records the cascade DELETE.

    Child-row counting is monkeypatched at `_count_rows_by_campaign`, so this fake only
    sees the existing-campaign select and (on a real delete) the bulk delete statement.
    """

    def __init__(self, campaign_rows):
        self._campaign_rows = list(campaign_rows)
        self.execute_count = 0
        self.commit_count = 0
        self.executed_stmts = []

    async def execute(self, stmt):
        self.execute_count += 1
        self.executed_stmts.append(stmt)
        if self.execute_count == 1:
            return _Result(self._campaign_rows)
        return _Result([])

    async def commit(self):
        self.commit_count += 1


def _campaign(campaign_id, name, start_parameter):
    return SimpleNamespace(id=campaign_id, name=name, start_parameter=start_parameter)


def _patch_counts(monkeypatch, reg_counts, start_counts):
    async def fake(_db, model, ids):
        source = reg_counts if model is AdvertisingCampaignRegistration else start_counts
        return {campaign_id: source.get(campaign_id, 0) for campaign_id in ids}

    monkeypatch.setattr(m, '_count_rows_by_campaign', fake)


def _deleted(session) -> bool:
    return any(isinstance(stmt, Delete) for stmt in session.executed_stmts)


# ---- grouped counter helper ----


@pytest.mark.asyncio
async def test_count_rows_by_campaign_empty_short_circuits():
    db = AsyncMock()

    result = await m._count_rows_by_campaign(db, AdvertisingCampaignRegistration, [])

    assert result == {}
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_count_rows_by_campaign_maps_grouped_counts():
    db = AsyncMock()
    db.execute.return_value = _Result([(1, 5), (2, 3)])

    result = await m._count_rows_by_campaign(db, AdvertisingCampaignStart, [1, 2])

    assert result == {1: 5, 2: 3}
    assert db.execute.await_count == 1


# ---- dry-run vs real delete ----


@pytest.mark.asyncio
async def test_bulk_delete_dry_run_counts_and_deletes_nothing(monkeypatch):
    _patch_counts(monkeypatch, {1: 5, 2: 0}, {1: 9, 2: 3})
    db = _FakeSession([_campaign(1, 'Alpha', 'tggr_a1_0'), _campaign(2, 'Beta', 'tggr_a1_1')])
    request = BulkCampaignDeleteRequest(ids=[1, 2], dry_run=True)

    resp = await m.delete_campaigns_bulk(request, MagicMock(id=7), db)

    assert resp.deleted_count is None
    assert {(d.id, d.registrations, d.starts) for d in resp.deletable} == {(1, 5, 9), (2, 0, 3)}
    assert {(d.id, d.name, d.start_parameter) for d in resp.deletable} == {
        (1, 'Alpha', 'tggr_a1_0'),
        (2, 'Beta', 'tggr_a1_1'),
    }
    assert resp.skipped == []
    assert resp.total_registrations == 5
    assert resp.total_starts == 12
    assert db.commit_count == 0
    assert db.execute_count == 1  # only the resolve-existing select
    assert not _deleted(db)


@pytest.mark.asyncio
async def test_bulk_delete_real_removes_and_reports_counts(monkeypatch):
    _patch_counts(monkeypatch, {1: 5, 2: 0}, {1: 9, 2: 3})
    db = _FakeSession([_campaign(1, 'Alpha', 'tggr_a1_0'), _campaign(2, 'Beta', 'tggr_a1_1')])
    request = BulkCampaignDeleteRequest(ids=[1, 2], dry_run=False)

    resp = await m.delete_campaigns_bulk(request, MagicMock(id=7), db)

    assert resp.deleted_count == 2
    assert resp.total_registrations == 5
    assert resp.total_starts == 12
    assert resp.skipped == []
    assert db.commit_count == 1
    assert _deleted(db)


@pytest.mark.asyncio
async def test_bulk_delete_mixed_existing_and_missing(monkeypatch):
    _patch_counts(monkeypatch, {1: 2}, {1: 4})
    db = _FakeSession([_campaign(1, 'Alpha', 'a')])
    request = BulkCampaignDeleteRequest(ids=[1, 999], dry_run=True)

    resp = await m.delete_campaigns_bulk(request, MagicMock(id=1), db)

    assert [d.id for d in resp.deletable] == [1]
    assert [s.id for s in resp.skipped] == [999]
    assert resp.skipped[0].reason == 'not_found'
    assert resp.total_registrations == 2
    assert resp.total_starts == 4
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_bulk_delete_real_only_deletes_existing_and_reports_skipped(monkeypatch):
    _patch_counts(monkeypatch, {1: 2}, {1: 4})
    db = _FakeSession([_campaign(1, 'Alpha', 'a')])
    request = BulkCampaignDeleteRequest(ids=[1, 999], dry_run=False)

    resp = await m.delete_campaigns_bulk(request, MagicMock(id=1), db)

    assert resp.deleted_count == 1
    assert [s.id for s in resp.skipped] == [999]
    assert _deleted(db)
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_bulk_delete_all_missing_real_deletes_nothing(monkeypatch):
    _patch_counts(monkeypatch, {}, {})
    db = _FakeSession([])  # nothing resolves
    request = BulkCampaignDeleteRequest(ids=[404, 405], dry_run=False)

    resp = await m.delete_campaigns_bulk(request, MagicMock(id=1), db)

    assert resp.deletable == []
    assert [s.id for s in resp.skipped] == [404, 405]
    assert resp.deleted_count == 0
    assert resp.total_registrations == 0
    assert resp.total_starts == 0
    assert not _deleted(db)  # empty id set -> no DELETE issued
    assert db.commit_count == 1  # transaction boundary still closes


@pytest.mark.asyncio
async def test_bulk_delete_dedups_repeated_ids(monkeypatch):
    _patch_counts(monkeypatch, {1: 3}, {1: 7})
    db = _FakeSession([_campaign(1, 'Alpha', 'a')])
    request = BulkCampaignDeleteRequest(ids=[1, 1, 1], dry_run=True)

    resp = await m.delete_campaigns_bulk(request, MagicMock(id=1), db)

    assert [d.id for d in resp.deletable] == [1]
    assert resp.skipped == []
    assert resp.total_registrations == 3  # counted once, no double-count
    assert resp.total_starts == 7


# ---- request schema size caps ----


def test_bulk_delete_request_rejects_more_than_500_ids():
    with pytest.raises(ValidationError):
        BulkCampaignDeleteRequest(ids=list(range(501)))

    assert len(BulkCampaignDeleteRequest(ids=list(range(500))).ids) == 500


def test_bulk_delete_request_rejects_empty_ids():
    with pytest.raises(ValidationError):
        BulkCampaignDeleteRequest(ids=[])


def test_bulk_delete_request_defaults_dry_run_false():
    assert BulkCampaignDeleteRequest(ids=[1]).dry_run is False


# ---- routing + RBAC ----


def _iter_dependants(dependant):
    yield dependant
    for sub in getattr(dependant, 'dependencies', []):
        yield from _iter_dependants(sub)


def _find_post_route(router, path):
    for route in router.routes:
        methods = getattr(route, 'methods', None) or set()
        if getattr(route, 'path', None) == path and 'POST' in methods:
            return route
    return None


def _find_delete_route(router, path):
    for route in router.routes:
        methods = getattr(route, 'methods', None) or set()
        if getattr(route, 'path', None) == path and 'DELETE' in methods:
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


def test_bulk_delete_route_registered_under_cabinet_prefix():
    from app.cabinet.routes import router

    assert _find_post_route(router, '/cabinet/admin/campaigns/bulk-delete') is not None


def test_bulk_delete_route_precedes_campaign_id_param_routes():
    from app.cabinet.routes import router

    bulk_delete_idx = _route_index(router, '/cabinet/admin/campaigns/bulk-delete', 'POST')
    detail_idx = _route_index(router, '/cabinet/admin/campaigns/{campaign_id}', 'DELETE')
    assert bulk_delete_idx != -1
    assert detail_idx != -1
    assert bulk_delete_idx < detail_idx


def test_bulk_delete_uses_campaigns_delete_like_single_delete():
    from app.cabinet.routes import router

    bulk_delete = _find_post_route(router, '/cabinet/admin/campaigns/bulk-delete')
    single_delete = _find_delete_route(router, '/cabinet/admin/campaigns/{campaign_id}')

    assert _required_permissions(bulk_delete) == {'campaigns:delete'}
    assert _required_permissions(bulk_delete) == _required_permissions(single_delete)


@pytest.mark.asyncio
async def test_bulk_delete_forbidden_for_non_admin(monkeypatch):
    from app.cabinet import dependencies as deps
    from app.cabinet.routes import router
    from app.services.permission_service import PermissionService

    guard = _require_permission_call(_find_post_route(router, '/cabinet/admin/campaigns/bulk-delete'))
    assert guard is not None

    monkeypatch.setattr(deps, 'get_client_ip', lambda _request: '127.0.0.1')
    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(False, 'denied')))
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())

    request = MagicMock()
    request.headers = {}

    with pytest.raises(HTTPException) as exc:
        await guard(request=request, user=MagicMock(id=1), db=AsyncMock())

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
