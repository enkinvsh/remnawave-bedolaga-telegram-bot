"""Tests for POST /cabinet/admin/campaigns/bulk (bulk campaign creation).

Covers the pure validate/dedup partition, the route's single-transaction insert
with `defaults` applied, the pydantic 500-item cap (surfaced as HTTP 422), and the
RBAC wiring (same `campaigns:create` guard as single-create; non-admin -> 403).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status
from pydantic import ValidationError

from app.cabinet.routes import admin_campaigns as m
from app.cabinet.schemas.campaigns import BulkCampaignCreateRequest, BulkCampaignItem


class _FakeScalarResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _FakeSession:
    def __init__(self, existing=()):
        self._existing = list(existing)
        self.added = []
        self.flush_count = 0
        self.commit_count = 0
        self._next_id = 0

    async def execute(self, _stmt):
        return _FakeScalarResult(self._existing)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flush_count += 1
        for obj in self.added:
            if getattr(obj, 'id', None) is None:
                self._next_id += 1
                obj.id = self._next_id

    async def commit(self):
        self.commit_count += 1


def _items(*params):
    return [BulkCampaignItem(name=f'name-{p}', start_parameter=p) for p in params]


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


def _require_permission_call(route):
    for dep in _iter_dependants(route.dependant):
        call = getattr(dep, 'call', None)
        if call is not None and getattr(call, '__qualname__', '').endswith('require_permission.<locals>.dependency'):
            return call
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


def test_partition_happy_path_all_valid():
    valid, skipped = m._partition_bulk_items(_items('tggr_a1_0', 'tggr_a1_1', 'tggr_a1_2'), set())

    assert [v.start_parameter for v in valid] == ['tggr_a1_0', 'tggr_a1_1', 'tggr_a1_2']
    assert skipped == []


def test_partition_dedup_against_existing():
    valid, skipped = m._partition_bulk_items(_items('dup', 'fresh'), {'dup'})

    assert [v.start_parameter for v in valid] == ['fresh']
    assert len(skipped) == 1
    assert skipped[0].start_parameter == 'dup'
    assert skipped[0].reason == 'duplicate_existing'


def test_partition_dedup_within_batch():
    valid, skipped = m._partition_bulk_items(_items('same', 'same'), set())

    assert [v.start_parameter for v in valid] == ['same']
    assert len(skipped) == 1
    assert skipped[0].reason == 'duplicate_in_batch'


def test_partition_invalid_start_parameter_skipped():
    valid, skipped = m._partition_bulk_items(
        [
            BulkCampaignItem(name='ok', start_parameter='has space'),
            BulkCampaignItem(name='ok', start_parameter='bad!char'),
            BulkCampaignItem(name='ok', start_parameter='x' * 65),
        ],
        set(),
    )

    assert valid == []
    assert {s.reason for s in skipped} == {'invalid_start_parameter'}


def test_partition_invalid_name_skipped():
    valid, skipped = m._partition_bulk_items(
        [
            BulkCampaignItem(name='', start_parameter='p1'),
            BulkCampaignItem(name='x' * 256, start_parameter='p2'),
        ],
        set(),
    )

    assert valid == []
    assert {s.reason for s in skipped} == {'invalid_name'}


def test_partition_start_parameter_dedup_is_case_exact():
    valid, skipped = m._partition_bulk_items(_items('Tggr_A1', 'tggr_a1'), set())

    assert [v.start_parameter for v in valid] == ['Tggr_A1', 'tggr_a1']
    assert skipped == []


@pytest.mark.asyncio
async def test_bulk_route_creates_in_single_transaction_with_defaults():
    db = _FakeSession()
    admin = MagicMock(id=7)
    request = BulkCampaignCreateRequest(items=_items('tggr_a1_0', 'tggr_a1_1', 'tggr_a1_2'))

    resp = await m.create_campaigns_bulk(request, admin, db)

    assert resp.created_count == 3
    assert resp.skipped_count == 0
    assert {c.start_parameter for c in resp.created} == {'tggr_a1_0', 'tggr_a1_1', 'tggr_a1_2'}
    assert all(c.id for c in resp.created)
    assert db.flush_count == 1
    assert db.commit_count == 1
    assert all(row.bonus_type == 'none' for row in db.added)
    assert all(row.is_active is True for row in db.added)
    assert all(row.created_by == 7 for row in db.added)


@pytest.mark.asyncio
async def test_bulk_route_skips_existing_duplicate_not_error():
    db = _FakeSession(existing=['tggr_a1_0'])
    request = BulkCampaignCreateRequest(items=_items('tggr_a1_0', 'tggr_a1_1'))

    resp = await m.create_campaigns_bulk(request, MagicMock(id=1), db)

    assert resp.created_count == 1
    assert resp.created[0].start_parameter == 'tggr_a1_1'
    assert resp.skipped_count == 1
    assert resp.skipped[0].reason == 'duplicate_existing'
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_bulk_route_skips_invalid_item_not_422():
    db = _FakeSession()
    request = BulkCampaignCreateRequest(
        items=[
            BulkCampaignItem(name='good', start_parameter='good_1'),
            BulkCampaignItem(name='bad', start_parameter='not valid'),
        ]
    )

    resp = await m.create_campaigns_bulk(request, MagicMock(id=1), db)

    assert resp.created_count == 1
    assert resp.created[0].start_parameter == 'good_1'
    assert resp.skipped_count == 1
    assert resp.skipped[0].reason == 'invalid_start_parameter'


def test_bulk_request_rejects_more_than_500_items():
    items = _items(*[f'p{i}' for i in range(501)])

    with pytest.raises(ValidationError):
        BulkCampaignCreateRequest(items=items)

    assert len(BulkCampaignCreateRequest(items=items[:500]).items) == 500


def test_bulk_route_registered_under_cabinet_prefix():
    from app.cabinet.routes import router

    assert _find_post_route(router, '/cabinet/admin/campaigns/bulk') is not None


def test_bulk_route_reuses_single_create_rbac_guard():
    from app.cabinet.routes import router

    bulk = _find_post_route(router, '/cabinet/admin/campaigns/bulk')
    single = _find_post_route(router, '/cabinet/admin/campaigns')

    bulk_perms = _required_permissions(bulk)
    assert 'campaigns:create' in bulk_perms
    assert bulk_perms == _required_permissions(single)


@pytest.mark.asyncio
async def test_bulk_route_forbidden_for_non_admin(monkeypatch):
    from app.cabinet import dependencies as deps
    from app.cabinet.routes import router
    from app.services.permission_service import PermissionService

    guard = _require_permission_call(_find_post_route(router, '/cabinet/admin/campaigns/bulk'))
    assert guard is not None

    monkeypatch.setattr(deps, 'get_client_ip', lambda _request: '127.0.0.1')
    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(False, 'denied')))
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())

    request = MagicMock()
    request.headers = {}

    with pytest.raises(HTTPException) as exc:
        await guard(request=request, user=MagicMock(id=1), db=AsyncMock())

    assert exc.value.status_code == status.HTTP_403_FORBIDDEN
