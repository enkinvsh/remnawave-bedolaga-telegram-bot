from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status

from app.cabinet.routes import admin_email_lifecycle as lifecycle_api, router
from app.config import settings as app_settings


def _find_route(path: str, method: str):
    for route in router.routes:
        methods = getattr(route, 'methods', None) or set()
        if getattr(route, 'path', None) == path and method in methods:
            return route
    return None


def _iter_dependants(dependant):
    yield dependant
    for dependency in getattr(dependant, 'dependencies', []):
        yield from _iter_dependants(dependency)


def _required_permissions(route) -> set[str]:
    permissions: set[str] = set()
    for dependency in _iter_dependants(route.dependant):
        call = getattr(dependency, 'call', None)
        if call is None or not getattr(call, '__qualname__', '').endswith('require_permission.<locals>.dependency'):
            continue
        freevars = call.__code__.co_freevars
        if 'permissions' in freevars and call.__closure__:
            permissions.update(call.__closure__[freevars.index('permissions')].cell_contents)
    return permissions


def test_email_lifecycle_routes_use_email_template_permissions():
    overview = _find_route('/cabinet/admin/email-lifecycle/overview', 'GET')
    settings = _find_route('/cabinet/admin/email-lifecycle/settings', 'PUT')
    rules = _find_route('/cabinet/admin/email-lifecycle/rules/{key}', 'PUT')

    assert overview is not None
    assert settings is not None
    assert rules is not None
    assert _required_permissions(overview) == {'email_templates:read'}
    assert _required_permissions(settings) == {'email_templates:edit'}
    assert _required_permissions(rules) == {'email_templates:edit'}


def test_email_masking_preserves_only_characters_after_first_two():
    mask_email = getattr(lifecycle_api, '_mask_email', None)

    assert mask_email is not None
    assert mask_email('longaddress@example.com') == 'lo*********@example.com'
    assert mask_email('jo@example.com') == 'jo@example.com'


async def test_overview_resolves_settings_stats_recent_masking_and_email_rules(monkeypatch: pytest.MonkeyPatch):
    sent_at = datetime(2026, 7, 21, 12, tzinfo=UTC)
    stats = SimpleNamespace(
        optout_count=2,
        audience_count=5,
        totals_30d={
            'trial_ending_email': 2,
            'post_trial_ladder_email': 1,
            'expired_discount_wave2_email': 0,
            'expired_discount_wave3_email': 0,
        },
        recent=[
            SimpleNamespace(
                event_key='trial_ending_email',
                user_id=17,
                email='longaddress@example.com',
                sent_at=sent_at,
            )
        ],
    )
    override = SimpleNamespace(key='trial_ending', enabled=False, config={'hours_before': 6})
    get_setting = AsyncMock(side_effect=['true', 'false'])
    monkeypatch.setattr(lifecycle_api, 'get_setting_value', get_setting, raising=False)
    monkeypatch.setattr(lifecycle_api, 'get_email_lifecycle_stats', AsyncMock(return_value=stats), raising=False)
    monkeypatch.setattr(lifecycle_api, 'get_all_rules', AsyncMock(return_value=[override]), raising=False)
    monkeypatch.setattr(app_settings, 'EMAIL_PROVIDER', 'postbox')

    response = await lifecycle_api.get_email_lifecycle_overview(MagicMock(), AsyncMock())

    payload = response.model_dump(mode='json')
    assert payload['enabled'] is True
    assert payload['layout_enabled'] is False
    assert payload['provider'] == 'postbox'
    assert payload['optout_count'] == 2
    assert payload['audience_count'] == 5
    assert payload['totals_30d']['trial_ending_email'] == 2
    assert payload['recent'] == [
        {
            'event_key': 'trial_ending_email',
            'user_id': 17,
            'email': 'lo*********@example.com',
            'sent_at': '2026-07-21T12:00:00Z',
        }
    ]
    assert [rule['key'] for rule in payload['rules']] == [
        'trial_ending',
        'post_trial_ladder',
        'expired_second_wave',
        'expired_third_wave',
    ]
    assert payload['rules'][0]['key'] == 'trial_ending'
    assert payload['rules'][0]['enabled'] is False
    assert payload['rules'][0]['config']['hours_before'] == 6
    assert 'message_template' in payload['rules'][0]['config']


async def test_settings_put_roundtrips_through_overview(monkeypatch: pytest.MonkeyPatch):
    stored: dict[str, str] = {}

    async def fake_upsert(_db, key: str, value: str):
        stored[key] = value

    async def fake_get(_db, key: str):
        return stored.get(key, 'false')

    monkeypatch.setattr(lifecycle_api, 'upsert_system_setting', fake_upsert, raising=False)
    monkeypatch.setattr(lifecycle_api, 'get_setting_value', fake_get, raising=False)
    monkeypatch.setattr(
        lifecycle_api,
        'get_email_lifecycle_stats',
        AsyncMock(return_value=SimpleNamespace(optout_count=0, audience_count=0, totals_30d={}, recent=[])),
        raising=False,
    )
    monkeypatch.setattr(lifecycle_api, 'get_all_rules', AsyncMock(return_value=[]), raising=False)
    db = AsyncMock()

    updated = await lifecycle_api.update_email_lifecycle_settings(
        lifecycle_api.EmailLifecycleSettingsUpdate(enabled=True), MagicMock(), db
    )
    overview = await lifecycle_api.get_email_lifecycle_overview(MagicMock(), db)

    assert updated.model_dump() == {'enabled': True}
    assert overview.enabled is True
    assert stored == {'CABINET_LIFECYCLE_EMAILS_ENABLED': 'true'}
    db.commit.assert_awaited_once()


async def test_rules_put_rejects_non_email_rule_key():
    with pytest.raises(HTTPException) as exc:
        await lifecycle_api.update_email_lifecycle_rule(
            'expired_1d',
            lifecycle_api.EmailLifecycleRuleUpdate(enabled=False),
            MagicMock(),
            AsyncMock(),
        )

    assert exc.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


async def test_rules_put_upserts_partial_override_and_returns_resolved_rule(monkeypatch: pytest.MonkeyPatch):
    existing = SimpleNamespace(key='expired_second_wave', enabled=True, config={'valid_hours': 48})
    upsert = AsyncMock(
        return_value=SimpleNamespace(
            key='expired_second_wave',
            enabled=False,
            config={'valid_hours': 48},
        )
    )
    reload_rules = AsyncMock()
    monkeypatch.setattr(lifecycle_api, 'get_rule', AsyncMock(return_value=existing), raising=False)
    monkeypatch.setattr(lifecycle_api, 'upsert_rule', upsert, raising=False)
    monkeypatch.setattr(lifecycle_api.NotificationSettingsService, 'reload', reload_rules, raising=False)
    db = AsyncMock()

    response = await lifecycle_api.update_email_lifecycle_rule(
        'expired_second_wave',
        lifecycle_api.EmailLifecycleRuleUpdate(enabled=False),
        MagicMock(telegram_id=99),
        db,
    )

    upsert.assert_awaited_once_with(db, 'expired_second_wave', False, {'valid_hours': 48})
    reload_rules.assert_awaited_once_with(db)
    assert response.model_dump() == {
        'key': 'expired_second_wave',
        'enabled': False,
        'config': {'discount_percent': 10, 'valid_hours': 48},
    }
