"""Тесты редактора строк локализации в кабинете.

Проверяем контракт роутера и то, что мутирующие ручки немедленно обновляют
in-memory кеш — без рестарта бота.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_locales
from app.cabinet.routes.admin_locales import (
    LocaleImportRequest,
    LocaleOverrideUpdate,
    delete_locale_string,
    export_locale_overrides,
    get_locale_string,
    import_locale_overrides,
    list_locale_strings,
    reload_locale_overrides,
    update_locale_string,
)
from app.localization.overrides import clear_override_cache, get_override


EXISTING_KEY = 'ACCESS_DENIED'
RU_DEFAULT = '❌ Доступ запрещен'


class _FakeRow:
    def __init__(self, key: str, language: str, value: str):
        self.key = key
        self.language = language
        self.value = value


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch):
    """Подменяет CRUD на in-memory словарь {(key, language): value}."""
    store: dict[tuple[str, str], str] = {}

    async def fake_list(_db):
        return [_FakeRow(k, lang, v) for (k, lang), v in store.items()]

    async def fake_get(_db, key: str, language: str):
        value = store.get((key, language))
        return None if value is None else _FakeRow(key, language, value)

    async def fake_upsert(_db, key: str, language: str, value: str):
        store[(key, language)] = value
        return _FakeRow(key, language, value)

    async def fake_delete(_db, key: str, language: str) -> bool:
        return store.pop((key, language), None) is not None

    monkeypatch.setattr(admin_locales, 'list_locale_overrides', fake_list)
    monkeypatch.setattr(admin_locales, 'get_locale_override', fake_get)
    monkeypatch.setattr(admin_locales, 'upsert_locale_override', fake_upsert)
    monkeypatch.setattr(admin_locales, 'delete_locale_override', fake_delete)
    monkeypatch.setattr('app.database.crud.locale_override.list_locale_overrides', fake_list)

    clear_override_cache()
    yield store
    clear_override_cache()


def _db() -> AsyncMock:
    return AsyncMock()


# ============ PUT / GET / DELETE ============


async def test_put_then_get_returns_override(fake_store):
    await update_locale_string(
        EXISTING_KEY,
        'ru',
        LocaleOverrideUpdate(value='Нельзя'),
        admin=None,
        db=_db(),
    )

    result = await get_locale_string(EXISTING_KEY, _admin=None, db=_db())

    assert result['key'] == EXISTING_KEY
    assert result['languages']['ru']['override_value'] == 'Нельзя'
    assert result['languages']['ru']['value'] == 'Нельзя'
    assert result['languages']['ru']['default_value'] == RU_DEFAULT
    assert result['languages']['ru']['is_overridden'] is True
    assert result['languages']['en']['is_overridden'] is False


async def test_put_refreshes_cache_immediately(fake_store):
    await update_locale_string(
        EXISTING_KEY,
        'ru',
        LocaleOverrideUpdate(value='Мгновенно'),
        admin=None,
        db=_db(),
    )

    assert get_override('ru', EXISTING_KEY) == 'Мгновенно'


async def test_delete_restores_default_and_refreshes_cache(fake_store):
    await update_locale_string(EXISTING_KEY, 'ru', LocaleOverrideUpdate(value='Нельзя'), admin=None, db=_db())

    result = await delete_locale_string(EXISTING_KEY, 'ru', admin=None, db=_db())

    assert result['was_overridden'] is True
    assert get_override('ru', EXISTING_KEY) is None

    fresh = await get_locale_string(EXISTING_KEY, _admin=None, db=_db())
    assert fresh['languages']['ru']['value'] == RU_DEFAULT
    assert fresh['languages']['ru']['is_overridden'] is False


async def test_delete_of_missing_override_is_noop(fake_store):
    result = await delete_locale_string(EXISTING_KEY, 'ru', admin=None, db=_db())
    assert result['was_overridden'] is False


# ============ Валидация ============


async def test_put_unknown_key_rejected(fake_store):
    with pytest.raises(HTTPException) as error:
        await update_locale_string('TOTALLY_MADE_UP_KEY', 'ru', LocaleOverrideUpdate(value='x'), admin=None, db=_db())
    assert 400 <= error.value.status_code < 500


async def test_put_unsupported_language_rejected(fake_store):
    with pytest.raises(HTTPException) as error:
        await update_locale_string(EXISTING_KEY, 'de', LocaleOverrideUpdate(value='x'), admin=None, db=_db())
    assert 400 <= error.value.status_code < 500


async def test_get_unknown_key_rejected(fake_store):
    with pytest.raises(HTTPException) as error:
        await get_locale_string('TOTALLY_MADE_UP_KEY', _admin=None, db=_db())
    assert 400 <= error.value.status_code < 500


def test_value_length_is_capped():
    with pytest.raises(ValueError):
        LocaleOverrideUpdate(value='x' * 4097)


# ============ Список и поиск ============


async def test_list_returns_defaults_with_override_flag(fake_store):
    result = await list_locale_strings(
        search=EXISTING_KEY,
        language='ru',
        only_overridden=False,
        limit=50,
        offset=0,
        _admin=None,
        db=_db(),
    )

    items = {item['key']: item for item in result['items']}
    assert EXISTING_KEY in items
    assert items[EXISTING_KEY]['default_value'] == RU_DEFAULT
    assert items[EXISTING_KEY]['is_overridden'] is False
    assert items[EXISTING_KEY]['override_value'] is None
    assert result['total'] >= 1


async def test_search_by_value_finds_the_key(fake_store):
    """Никто не помнит ключей — помнят формулировку, которую видели."""
    result = await list_locale_strings(
        search='доступ запрещен',
        language='ru',
        only_overridden=False,
        limit=100,
        offset=0,
        _admin=None,
        db=_db(),
    )

    assert EXISTING_KEY in {item['key'] for item in result['items']}


async def test_search_by_value_matches_the_override_text(fake_store):
    await update_locale_string(EXISTING_KEY, 'ru', LocaleOverrideUpdate(value='Вход воспрещён'), admin=None, db=_db())

    result = await list_locale_strings(
        search='воспрещён',
        language='ru',
        only_overridden=False,
        limit=100,
        offset=0,
        _admin=None,
        db=_db(),
    )

    assert EXISTING_KEY in {item['key'] for item in result['items']}


async def test_only_overridden_filters(fake_store):
    await update_locale_string(EXISTING_KEY, 'ru', LocaleOverrideUpdate(value='Нельзя'), admin=None, db=_db())

    result = await list_locale_strings(
        search=None,
        language='ru',
        only_overridden=True,
        limit=100,
        offset=0,
        _admin=None,
        db=_db(),
    )

    assert [item['key'] for item in result['items']] == [EXISTING_KEY]
    assert result['total'] == 1


async def test_list_pagination(fake_store):
    first = await list_locale_strings(
        search=None, language='ru', only_overridden=False, limit=5, offset=0, _admin=None, db=_db()
    )
    second = await list_locale_strings(
        search=None, language='ru', only_overridden=False, limit=5, offset=5, _admin=None, db=_db()
    )

    assert len(first['items']) == 5
    assert len(second['items']) == 5
    assert first['total'] == second['total']
    assert {i['key'] for i in first['items']}.isdisjoint({i['key'] for i in second['items']})


async def test_list_rejects_unsupported_language(fake_store):
    with pytest.raises(HTTPException) as error:
        await list_locale_strings(
            search=None, language='de', only_overridden=False, limit=10, offset=0, _admin=None, db=_db()
        )
    assert 400 <= error.value.status_code < 500


# ============ Reload / export / import ============


async def test_reload_refreshes_cache_from_db(fake_store):
    fake_store[(EXISTING_KEY, 'ru')] = 'Прямо из БД'
    assert get_override('ru', EXISTING_KEY) is None

    result = await reload_locale_overrides(admin=None, db=_db())

    assert result['loaded'] == 1
    assert get_override('ru', EXISTING_KEY) == 'Прямо из БД'


async def test_export_import_roundtrip(fake_store):
    await update_locale_string(EXISTING_KEY, 'ru', LocaleOverrideUpdate(value='Отказано'), admin=None, db=_db())
    await update_locale_string(EXISTING_KEY, 'en', LocaleOverrideUpdate(value='Refused'), admin=None, db=_db())

    exported = await export_locale_overrides(_admin=None, db=_db())
    assert exported['overrides'] == {'ru': {EXISTING_KEY: 'Отказано'}, 'en': {EXISTING_KEY: 'Refused'}}

    fake_store.clear()
    clear_override_cache()

    imported = await import_locale_overrides(LocaleImportRequest(overrides=exported['overrides']), admin=None, db=_db())

    assert imported['created'] == 2
    assert imported['updated'] == 0

    reexported = await export_locale_overrides(_admin=None, db=_db())
    assert reexported['overrides'] == exported['overrides']


async def test_import_is_idempotent(fake_store):
    payload = {'ru': {EXISTING_KEY: 'Отказано'}}

    first = await import_locale_overrides(LocaleImportRequest(overrides=payload), admin=None, db=_db())
    second = await import_locale_overrides(LocaleImportRequest(overrides=payload), admin=None, db=_db())

    assert first == {
        'status': 'ok',
        'created': 1,
        'updated': 0,
        'skipped_unknown_key': 0,
        'skipped_unknown_language': 0,
    }
    assert second == {
        'status': 'ok',
        'created': 0,
        'updated': 1,
        'skipped_unknown_key': 0,
        'skipped_unknown_language': 0,
    }
    assert len(fake_store) == 1


async def test_import_skips_unknown_keys_and_languages(fake_store):
    result = await import_locale_overrides(
        LocaleImportRequest(
            overrides={
                'ru': {EXISTING_KEY: 'ok', 'NOT_A_REAL_KEY': 'nope'},
                'de': {EXISTING_KEY: 'nope'},
            }
        ),
        admin=None,
        db=_db(),
    )

    assert result['created'] == 1
    assert result['skipped_unknown_key'] == 1
    assert result['skipped_unknown_language'] == 1
    assert fake_store == {(EXISTING_KEY, 'ru'): 'ok'}


async def test_import_refreshes_cache(fake_store):
    await import_locale_overrides(
        LocaleImportRequest(overrides={'ru': {EXISTING_KEY: 'Из импорта'}}), admin=None, db=_db()
    )

    assert get_override('ru', EXISTING_KEY) == 'Из импорта'


# ============ Регистрация роутера ============


def test_router_is_registered_in_cabinet():
    from app.cabinet.routes import router as cabinet_router

    paths = {route.path for route in cabinet_router.routes}
    assert '/cabinet/admin/locales' in paths
    assert '/cabinet/admin/locales/export' in paths


def test_export_route_declared_before_key_route():
    """Иначе GET /export попадёт в /{key} и вернёт 404 по несуществующему ключу."""
    paths = [route.path for route in admin_locales.router.routes]
    assert paths.index('/admin/locales/export') < paths.index('/admin/locales/{key}')
