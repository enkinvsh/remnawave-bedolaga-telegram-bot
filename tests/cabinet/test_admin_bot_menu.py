"""Тесты моста кабинета к конструктору меню БОТА (`menu_layout_config`).

Роутер — тонкая обёртка над `MenuLayoutService`: та же логика, что и у админского
REST API (`/menu-layout` в `app/webapi`), но с авторизацией и сессией кабинета.

ВАЖНО: это НЕ `admin_menu_layout` — тот правит `CABINET_MENU_LAYOUT` (меню самого
кабинета). Отдельный тест ниже фиксирует, что две системы не пересекаются.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.admin_bot_menu import (
    get_bot_menu_layout,
    list_bot_menu_builtin_buttons,
    reset_bot_menu_layout,
    update_bot_menu_layout,
)
from app.services.menu_layout.constants import MENU_LAYOUT_CONFIG_KEY
from app.services.menu_layout.schemas import (
    MenuButtonConfig,
    MenuLayoutUpdateRequest,
    MenuRowConfig,
)
from app.services.menu_layout.service import MenuLayoutService
from app.utils.menu_layout_cache import MENU_LAYOUT_KEY


BUILTIN_BUTTON_IDS = {
    'connect',
    'happ_download',
    'subscription',
    'buy_traffic',
    'balance',
    'trial',
    'buy_subscription',
    'simple_subscription',
    'resume_checkout',
    'promocode',
    'referrals',
    'contests',
    'support',
    'info',
    'language',
    'admin_panel',
    'moderator_panel',
}

BOT_MENU_PATHS = {
    ('GET', '/cabinet/admin/bot-menu'),
    ('PUT', '/cabinet/admin/bot-menu'),
    ('POST', '/cabinet/admin/bot-menu/reset'),
    ('GET', '/cabinet/admin/bot-menu/builtin-buttons'),
    ('GET', '/cabinet/admin/bot-menu/available-callbacks'),
    ('GET', '/cabinet/admin/bot-menu/placeholders'),
    ('POST', '/cabinet/admin/bot-menu/preview'),
}


# ---- Фейковое хранилище SystemSetting -----------------------------------------


class _FakeResult:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


class _FakeDB:
    """Минимальная замена AsyncSession для `select(SystemSetting).where(key == ...)`."""

    def __init__(self, store: dict):
        self.store = store

    async def execute(self, statement):
        key = statement.whereclause.right.value
        return _FakeResult(self.store.get(key))

    def add(self, obj):
        self.store[obj.key] = obj

    async def flush(self):
        """Фейковая сессия ничего не сбрасывает на диск."""

    async def commit(self):
        """Фейковая сессия ничего не коммитит."""


@pytest.fixture
def store():
    """Чистое хранилище настроек + сброс кеша сервиса до и после теста."""
    MenuLayoutService.invalidate_cache()
    data: dict = {}
    yield data
    MenuLayoutService.invalidate_cache()


@pytest.fixture(autouse=True)
def audit(monkeypatch):
    """Перехват записи в админский журнал.

    Фейковая сессия не умеет работать с ORM-моделью `AdminAuditLog`, а тестам
    важен сам факт и содержимое вызова, а не строка в таблице.
    """
    from app.services.permission_service import PermissionService

    recorder = AsyncMock()
    monkeypatch.setattr(PermissionService, 'log_action', recorder)
    return recorder


def _db(store: dict) -> _FakeDB:
    return _FakeDB(store)


def _admin():
    return SimpleNamespace(id=1, telegram_id=468130024)


def _valid_payload() -> MenuLayoutUpdateRequest:
    return MenuLayoutUpdateRequest(
        rows=[MenuRowConfig(id='row_custom', buttons=['btn_custom'], max_per_row=1)],
        buttons={
            'btn_custom': MenuButtonConfig(
                type='callback',
                text={'ru': 'Кнопка', 'en': 'Button'},
                action='menu_custom',
            )
        },
    )


# ---- 1. GET без сохранённой конфигурации отдаёт дефолт ------------------------


async def test_get_returns_default_layout_when_nothing_stored(store):
    result = await get_bot_menu_layout(_admin=_admin(), db=_db(store))

    assert len(result.rows) == 13
    assert len(result.buttons) == 17


# ---- 2. PUT сохраняет, GET возвращает сохранённое -----------------------------


async def test_put_persists_config_and_get_returns_it(store):
    await update_bot_menu_layout(_valid_payload(), admin=_admin(), db=_db(store))

    assert MENU_LAYOUT_CONFIG_KEY in store

    fresh = await get_bot_menu_layout(_admin=_admin(), db=_db(store))

    assert [row.id for row in fresh.rows] == ['row_custom']
    assert set(fresh.buttons) == {'btn_custom'}
    assert fresh.buttons['btn_custom'].action == 'menu_custom'


# ---- 3. PUT с невалидной конфигурацией не сохраняет ---------------------------


async def test_put_rejects_invalid_config_without_persisting(store):
    broken = MenuLayoutUpdateRequest(
        rows=[MenuRowConfig(id='row_broken', buttons=['ghost_button'])],
        buttons={},
    )

    with pytest.raises(HTTPException) as error:
        await update_bot_menu_layout(broken, admin=_admin(), db=_db(store))

    assert 400 <= error.value.status_code < 500
    assert MENU_LAYOUT_CONFIG_KEY not in store


async def test_put_invalid_config_leaves_previous_config_intact(store):
    await update_bot_menu_layout(_valid_payload(), admin=_admin(), db=_db(store))
    saved = store[MENU_LAYOUT_CONFIG_KEY].value

    broken = MenuLayoutUpdateRequest(
        rows=[MenuRowConfig(id='row_broken', buttons=['ghost_button'])],
        buttons={},
    )
    with pytest.raises(HTTPException):
        await update_bot_menu_layout(broken, admin=_admin(), db=_db(store))

    assert store[MENU_LAYOUT_CONFIG_KEY].value == saved


# ---- 4. Сброс к дефолту -------------------------------------------------------


async def test_reset_restores_default_layout(store):
    await update_bot_menu_layout(_valid_payload(), admin=_admin(), db=_db(store))

    result = await reset_bot_menu_layout(admin=_admin(), db=_db(store))

    assert len(result.rows) == 13
    assert len(result.buttons) == 17

    fresh = await get_bot_menu_layout(_admin=_admin(), db=_db(store))
    assert len(fresh.rows) == 13


# ---- 5. Каталог встроенных кнопок --------------------------------------------


async def test_builtin_buttons_catalogue_is_complete():
    result = await list_bot_menu_builtin_buttons(_admin=_admin())

    assert result.total == 17
    assert {item.id for item in result.items} == BUILTIN_BUTTON_IDS


# ---- 6. Каждая ручка закрыта правами кабинета ---------------------------------


def _iter_dependants(dependant):
    yield dependant
    for sub in getattr(dependant, 'dependencies', []):
        yield from _iter_dependants(sub)


def _find_route(router, path: str, method: str):
    for route in router.routes:
        methods = getattr(route, 'methods', None) or set()
        if getattr(route, 'path', None) == path and method in methods:
            return route
    return None


def _required_permissions(route) -> set[str]:
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


def test_every_bot_menu_route_is_permission_guarded():
    from app.cabinet.routes import router

    for method, path in BOT_MENU_PATHS:
        route = _find_route(router, path, method)
        assert route is not None, f'{method} {path} не зарегистрирован'
        assert _required_permissions(route), f'{method} {path} без проверки прав'


def test_bot_menu_uses_its_own_permission_not_settings():
    """Меню бота — отдельное право: доступ к логотипу не должен давать доступ к меню."""
    from app.cabinet.routes import router

    read_route = _find_route(router, '/cabinet/admin/bot-menu', 'GET')
    write_route = _find_route(router, '/cabinet/admin/bot-menu', 'PUT')
    reset_route = _find_route(router, '/cabinet/admin/bot-menu/reset', 'POST')

    assert _required_permissions(read_route) == {'bot_menu:read'}
    assert _required_permissions(write_route) == {'bot_menu:edit'}
    assert _required_permissions(reset_route) == {'bot_menu:edit'}


def test_no_bot_menu_route_still_relies_on_settings_permission():
    from app.cabinet.routes import router

    for method, path in BOT_MENU_PATHS:
        perms = _required_permissions(_find_route(router, path, method))
        assert perms, f'{method} {path} без проверки прав'
        assert all(perm.startswith('bot_menu:') for perm in perms), f'{method} {path} держится за {perms}'


def test_bot_menu_permission_is_registered_in_the_catalogue():
    """Без записи в реестре роль с этим правом нельзя было бы даже создать."""
    from app.services.permission_service import PERMISSION_REGISTRY, get_all_permissions

    assert PERMISSION_REGISTRY['bot_menu'] == ['read', 'edit']
    assert {'bot_menu:read', 'bot_menu:edit'} <= set(get_all_permissions())


def test_bot_menu_permission_is_granted_to_the_admin_preset_only():
    """Право выдаётся осознанно: явно его получает только пресет Admin.

    Admin (level=100) и так держит `users:*`, `payments:*`, `roles:*` — отказ именно в
    меню бота ничего не защищал бы, зато требовал ручной выдачи в каждом тенанте.
    Moderator / Marketer / Support права не получают, поэтому кастомная роль с одним
    лишь `settings:edit` (дизайнер на брендинге) до меню бота по-прежнему не достаёт.
    """
    from app.services.permission_service import permission_matches
    from app.services.rbac_bootstrap_service import _PRESET_ROLES

    explicit = {
        preset['name'] for preset in _PRESET_ROLES if any(perm.startswith('bot_menu') for perm in preset['permissions'])
    }
    assert explicit == {'Admin'}

    reachable = {
        preset['name']
        for preset in _PRESET_ROLES
        if any(permission_matches(perm, 'bot_menu:edit') for perm in preset['permissions'])
    }
    assert reachable == {'Superadmin', 'Admin'}


@pytest.mark.parametrize(
    ('held', 'allowed'),
    [
        ('settings:edit', False),
        ('settings:*', False),
        ('bot_menu:edit', True),
        ('bot_menu:*', True),
        ('*:*', True),
    ],
)
def test_settings_edit_no_longer_reaches_the_bot_menu(held: str, allowed: bool):
    from app.services.permission_service import permission_matches

    assert permission_matches(held, 'bot_menu:edit') is allowed


@pytest.mark.asyncio
async def test_bot_menu_rejects_caller_without_permission(monkeypatch):
    from app.cabinet import dependencies as deps
    from app.cabinet.routes import router
    from app.services.permission_service import PermissionService

    guard = _require_permission_call(_find_route(router, '/cabinet/admin/bot-menu', 'GET'))
    assert guard is not None

    monkeypatch.setattr(deps, 'get_client_ip', lambda _request: '127.0.0.1')
    monkeypatch.setattr(PermissionService, 'check_permission', AsyncMock(return_value=(False, 'denied')))
    monkeypatch.setattr(PermissionService, 'log_action', AsyncMock())

    request = MagicMock()
    request.headers = {}

    with pytest.raises(HTTPException) as error:
        await guard(request=request, user=MagicMock(id=1), db=AsyncMock())

    assert error.value.status_code in (401, 403)


# ---- 6a. PUT не пропускает враждебную ссылку ---------------------------------


def _url_payload(action: str) -> MenuLayoutUpdateRequest:
    return MenuLayoutUpdateRequest(
        rows=[MenuRowConfig(id='row_links', buttons=['btn_link'], max_per_row=1)],
        buttons={'btn_link': MenuButtonConfig(type='url', text={'ru': 'Ссылка'}, action=action)},
    )


@pytest.mark.parametrize('action', ['http://evil.com', 'javascript:alert(1)', 'https://user:pass@evil.com'])
async def test_put_rejects_hostile_url_with_400_and_does_not_persist(store, action: str):
    with pytest.raises(HTTPException) as error:
        await update_bot_menu_layout(_url_payload(action), admin=_admin(), db=_db(store))

    assert error.value.status_code == 400
    assert any(item['field'] == 'buttons.btn_link.action' for item in error.value.detail['errors'])
    assert MENU_LAYOUT_CONFIG_KEY not in store


async def test_put_accepts_https_url(store):
    result = await update_bot_menu_layout(_url_payload('https://dropweb.org'), admin=_admin(), db=_db(store))

    assert result.buttons['btn_link'].action == 'https://dropweb.org'
    assert MENU_LAYOUT_CONFIG_KEY in store


# ---- 6b. Аудит изменения ------------------------------------------------------


async def test_successful_put_is_written_to_the_audit_log(store, audit):
    await update_bot_menu_layout(_url_payload('https://dropweb.org'), admin=_admin(), db=_db(store))

    audit.assert_awaited_once()
    kwargs = audit.await_args.kwargs
    assert kwargs['user_id'] == 1
    assert kwargs['action'] == 'bot_menu_update'
    assert kwargs['resource_type'] == 'bot_menu'
    assert kwargs['resource_id'] == MENU_LAYOUT_CONFIG_KEY
    assert kwargs['details']['external_urls'] == ['https://dropweb.org']


async def test_reset_is_written_to_the_audit_log(store, audit):
    await reset_bot_menu_layout(admin=_admin(), db=_db(store))

    audit.assert_awaited_once()
    assert audit.await_args.kwargs['action'] == 'bot_menu_reset'
    assert audit.await_args.kwargs['user_id'] == 1


async def test_rejected_put_is_not_audited(store, audit):
    with pytest.raises(HTTPException):
        await update_bot_menu_layout(_url_payload('javascript:alert(1)'), admin=_admin(), db=_db(store))

    audit.assert_not_awaited()


# ---- 7. Две системы меню не пересекаются --------------------------------------


def test_bot_menu_router_does_not_collide_with_cabinet_menu_layout():
    from app.cabinet.routes import router

    paths = {route.path for route in router.routes}

    assert '/cabinet/admin/bot-menu' in paths
    assert '/cabinet/admin/menu-layout' in paths
    assert MENU_LAYOUT_CONFIG_KEY != MENU_LAYOUT_KEY


async def test_editing_bot_menu_does_not_touch_cabinet_menu_layout(store):
    await update_bot_menu_layout(_valid_payload(), admin=_admin(), db=_db(store))

    assert MENU_LAYOUT_CONFIG_KEY in store
    assert MENU_LAYOUT_KEY not in store


async def test_editing_cabinet_menu_layout_does_not_touch_bot_menu(store, monkeypatch):
    from app.cabinet.routes import admin_menu_layout

    monkeypatch.setattr(admin_menu_layout, 'load_button_styles_cache', AsyncMock())
    monkeypatch.setattr(admin_menu_layout, 'load_menu_layout_cache', AsyncMock())

    await update_bot_menu_layout(_valid_payload(), admin=_admin(), db=_db(store))
    bot_menu_snapshot = store[MENU_LAYOUT_CONFIG_KEY].value

    await admin_menu_layout.update_menu_layout(
        admin_menu_layout.MenuConfigUpdateRequest(
            rows=[
                admin_menu_layout.RowConfig(
                    id='row_1',
                    max_per_row=2,
                    buttons=[admin_menu_layout.ButtonConfig(id='subscription', type='builtin')],
                )
            ]
        ),
        admin=_admin(),
        db=_db(store),
    )

    assert MENU_LAYOUT_KEY in store
    assert store[MENU_LAYOUT_CONFIG_KEY].value == bot_menu_snapshot
