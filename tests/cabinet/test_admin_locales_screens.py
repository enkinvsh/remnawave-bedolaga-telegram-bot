"""Экраны в редакторе локалей: список и превью.

Плоский список из 1966 ключей нечитаем для человека. Эти ручки дают
экрано-ориентированный вход: выбрать экран, увидеть его так, как рендерит бот,
и править только те строки, из которых он собран.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes.admin_locales import (
    ScreenPreviewRequest,
    list_preview_screens,
    preview_locale_screen,
    router,
)
from app.localization.overrides import clear_override_cache, get_override_cache, set_override_cache


STATUS_KEY = 'SUB_STATUS_ACTIVE_LONG'


@pytest.fixture(autouse=True)
def _clean_override_cache():
    clear_override_cache()
    yield
    clear_override_cache()


def _db() -> AsyncMock:
    return AsyncMock()


def _first_matching_route(path: str, method: str):
    """Роут, который реально обработает запрос — первый совпавший, как в FastAPI."""
    scope = {'type': 'http', 'path': path, 'method': method, 'headers': [], 'root_path': ''}
    for route in router.routes:
        match, _ = route.matches(scope)
        if match.name != 'NONE':
            return route
    return None


# ============ Порядок роутов: /screens не должен уехать в /{key} ============


def test_screens_route_wins_over_key_route():
    route = _first_matching_route('/admin/locales/screens', 'GET')

    assert route is not None
    assert route.endpoint is list_preview_screens


def test_preview_route_is_reachable():
    route = _first_matching_route('/admin/locales/screens/main_menu/preview', 'POST')

    assert route is not None
    assert route.endpoint is preview_locale_screen


def test_key_route_still_works_for_a_normal_key():
    route = _first_matching_route('/admin/locales/ACCESS_DENIED', 'GET')

    assert route is not None
    assert route.endpoint is not list_preview_screens


# ============ GET /screens ============


async def test_list_screens_contains_main_menu():
    result = await list_preview_screens(_admin=None)

    screens = {screen['id']: screen for screen in result['screens']}
    assert 'main_menu' in screens
    assert screens['main_menu']['title']
    assert screens['main_menu']['description']
    assert result['total'] == len(result['screens'])


async def test_list_screens_reports_available_languages():
    result = await list_preview_screens(_admin=None)

    assert 'ru' in result['available_languages']


async def test_list_screens_offers_states():
    result = await list_preview_screens(_admin=None)
    main_menu = next(screen for screen in result['screens'] if screen['id'] == 'main_menu')

    state_ids = [state['id'] for state in main_menu['states']]
    assert 'expired' in state_ids
    assert 'none' in state_ids
    assert all(state['label'] for state in main_menu['states'])
    assert main_menu['default_state'] in state_ids


async def test_list_screens_declares_editable_keys():
    result = await list_preview_screens(_admin=None)
    main_menu = next(screen for screen in result['screens'] if screen['id'] == 'main_menu')

    assert 'MAIN_MENU_TARIFF_LINE' in main_menu['keys']
    assert 'SUB_STATUS_EXPIRED' in main_menu['keys']


# ============ POST /screens/{id}/preview ============


async def test_preview_returns_text_and_keys():
    result = await preview_locale_screen(
        'main_menu',
        ScreenPreviewRequest(language='ru'),
        _admin=None,
        db=_db(),
    )

    assert result['screen_id'] == 'main_menu'
    assert result['language'] == 'ru'
    assert result['text']

    keys = [entry['key'] for entry in result['keys']]
    assert 'MAIN_MENU' in keys
    assert any(key.startswith('SUB_STATUS_') for key in keys), keys
    assert all({'key', 'default_value', 'override_value', 'value'} <= set(entry) for entry in result['keys'])


async def test_preview_reflects_saved_override():
    set_override_cache({('ru', STATUS_KEY): '💎 СОХРАНЁННЫЙ'})

    result = await preview_locale_screen(
        'main_menu',
        ScreenPreviewRequest(language='ru'),
        _admin=None,
        db=_db(),
    )

    assert '💎 СОХРАНЁННЫЙ' in result['text']


async def test_preview_applies_draft_without_saving_it():
    before = get_override_cache()

    result = await preview_locale_screen(
        'main_menu',
        ScreenPreviewRequest(language='ru', draft={STATUS_KEY: '💎 ЧЕРНОВИК'}),
        _admin=None,
        db=_db(),
    )

    assert '💎 ЧЕРНОВИК' in result['text']
    assert get_override_cache() == before


async def test_preview_rejects_unknown_screen():
    with pytest.raises(HTTPException) as error:
        await preview_locale_screen(
            'no_such_screen',
            ScreenPreviewRequest(language='ru'),
            _admin=None,
            db=_db(),
        )

    assert error.value.status_code == 404


async def test_preview_rejects_unsupported_language():
    with pytest.raises(HTTPException) as error:
        await preview_locale_screen(
            'main_menu',
            ScreenPreviewRequest(language='klingon'),
            _admin=None,
            db=_db(),
        )

    assert error.value.status_code == 400


async def test_preview_accepts_a_state_and_echoes_it():
    result = await preview_locale_screen(
        'main_menu',
        ScreenPreviewRequest(language='ru', state='expired'),
        _admin=None,
        db=_db(),
    )

    assert result['state'] == 'expired'
    rendered = [entry['key'] for entry in result['keys'] if entry['rendered']]
    assert 'SUB_STATUS_EXPIRED' in rendered


async def test_preview_defaults_to_the_active_state():
    result = await preview_locale_screen(
        'main_menu',
        ScreenPreviewRequest(language='ru'),
        _admin=None,
        db=_db(),
    )

    assert result['state'] == 'active_long'


async def test_preview_rejects_unknown_state():
    with pytest.raises(HTTPException) as error:
        await preview_locale_screen(
            'main_menu',
            ScreenPreviewRequest(language='ru', state='no_such_state'),
            _admin=None,
            db=_db(),
        )

    assert error.value.status_code == 400


async def test_preview_offers_keys_that_this_state_does_not_render():
    result = await preview_locale_screen(
        'main_menu',
        ScreenPreviewRequest(language='ru', state='active_long'),
        _admin=None,
        db=_db(),
    )

    entry = next(item for item in result['keys'] if item['key'] == 'MAIN_MENU_TARIFF_LINE')
    assert entry['rendered'] is False
    assert entry['value']


async def test_preview_rejects_oversized_draft_value():
    with pytest.raises(HTTPException) as error:
        await preview_locale_screen(
            'main_menu',
            ScreenPreviewRequest(language='ru', draft={STATUS_KEY: 'x' * 5000}),
            _admin=None,
            db=_db(),
        )

    assert error.value.status_code == 400
