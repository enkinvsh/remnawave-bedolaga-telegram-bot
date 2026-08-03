"""Тесты слоя админских override'ов строк локализации.

Ключевое требование: чтение строки (`Texts._get_value`) остаётся синхронным и
БЕЗ I/O — оно выполняется для каждой строки каждого сообщения бота.
"""

import pytest

from app.localization import overrides as overrides_module
from app.localization.overrides import (
    clear_override_cache,
    get_override,
    get_override_cache,
    load_overrides,
    set_override_cache,
)
from app.localization.texts import Texts


EXISTING_KEY = 'ACCESS_DENIED'


@pytest.fixture(autouse=True)
def _clean_override_cache():
    clear_override_cache()
    yield
    clear_override_cache()


# ============ Чтение через Texts ============


def test_get_value_returns_override_when_present():
    set_override_cache({('ru', EXISTING_KEY): 'Кастомный отказ'})

    assert Texts('ru')[EXISTING_KEY] == 'Кастомный отказ'


def test_get_value_falls_back_to_bundled_value_when_no_override():
    bundled = Texts('ru')[EXISTING_KEY]

    set_override_cache({('ru', 'SOME_OTHER_KEY'): 'x'})

    assert Texts('ru')[EXISTING_KEY] == bundled


def test_ru_override_does_not_leak_into_en():
    en_bundled = Texts('en')[EXISTING_KEY]

    set_override_cache({('ru', EXISTING_KEY): 'Только для русского'})

    assert Texts('ru')[EXISTING_KEY] == 'Только для русского'
    assert Texts('en')[EXISTING_KEY] == en_bundled


def test_override_wins_over_dynamic_value():
    """TRAFFIC_* и SUPPORT_INFO считаются в _build_dynamic_values.

    Админ явно просил, чтобы override перебивал и их — это значит, что он
    забирает на себя форматирование цены для этого ключа.
    """
    dynamic_default = Texts('ru')['TRAFFIC_10GB']
    assert dynamic_default

    set_override_cache({('ru', 'TRAFFIC_10GB'): '10 гигов задёшево'})

    assert Texts('ru')['TRAFFIC_10GB'] == '10 гигов задёшево'


def test_override_applies_to_attribute_and_get_and_t():
    set_override_cache({('ru', EXISTING_KEY): 'Стоп'})
    texts = Texts('ru')

    assert texts.ACCESS_DENIED == 'Стоп'
    assert texts.get(EXISTING_KEY) == 'Стоп'
    assert texts.t(EXISTING_KEY) == 'Стоп'


def test_get_value_does_no_io(monkeypatch: pytest.MonkeyPatch):
    """Если бы чтение строки лезло в БД, этот тест упал бы."""
    import app.database.database as database_module

    def _explode(*_args, **_kwargs):
        raise AssertionError('_get_value обратился к базе данных')

    monkeypatch.setattr(database_module, 'AsyncSessionLocal', _explode)
    set_override_cache({('ru', EXISTING_KEY): 'Без БД'})

    assert Texts('ru')[EXISTING_KEY] == 'Без БД'
    assert Texts('ru')['TRAFFIC_10GB']


# ============ Кеш ============


def test_get_override_returns_none_for_unknown():
    assert get_override('ru', 'NOPE') is None


def test_set_override_cache_replaces_previous_content():
    set_override_cache({('ru', 'A'): '1'})
    set_override_cache({('en', 'B'): '2'})

    assert get_override('ru', 'A') is None
    assert get_override('en', 'B') == '2'


def test_clear_override_cache_empties_it():
    set_override_cache({('ru', 'A'): '1'})
    clear_override_cache()

    assert get_override_cache() == {}


def test_get_override_cache_returns_a_copy():
    set_override_cache({('ru', 'A'): '1'})
    snapshot = get_override_cache()
    snapshot[('ru', 'A')] = 'mutated'

    assert get_override('ru', 'A') == '1'


# ============ load_overrides ============


async def test_load_overrides_fills_cache_and_returns_count(monkeypatch: pytest.MonkeyPatch):
    class _Row:
        def __init__(self, key, language, value):
            self.key = key
            self.language = language
            self.value = value

    async def fake_list(_db):
        return [_Row(EXISTING_KEY, 'ru', 'Из БД'), _Row(EXISTING_KEY, 'en', 'From DB')]

    monkeypatch.setattr('app.database.crud.locale_override.list_locale_overrides', fake_list)

    loaded = await load_overrides(db=object())

    assert loaded == 2
    assert get_override('ru', EXISTING_KEY) == 'Из БД'
    assert Texts('en')[EXISTING_KEY] == 'From DB'


async def test_load_overrides_with_broken_db_keeps_previous_cache(monkeypatch: pytest.MonkeyPatch):
    set_override_cache({('ru', EXISTING_KEY): 'Старое значение'})

    async def boom(_db):
        raise RuntimeError('db is down')

    monkeypatch.setattr('app.database.crud.locale_override.list_locale_overrides', boom)

    result = await load_overrides(db=object())

    assert result == 1
    assert get_override('ru', EXISTING_KEY) == 'Старое значение'
    assert Texts('ru')[EXISTING_KEY] == 'Старое значение'


async def test_load_overrides_without_session_uses_session_factory(monkeypatch: pytest.MonkeyPatch):
    """Без переданной сессии открывает свою — и не падает, если БД недоступна."""
    import app.database.database as database_module

    def _explode(*_args, **_kwargs):
        raise RuntimeError('no db')

    monkeypatch.setattr(database_module, 'AsyncSessionLocal', _explode)

    assert await load_overrides() == 0


def test_overrides_module_has_no_framework_imports():
    """Слой чтения обязан оставаться чистым: без FastAPI и aiogram."""
    import ast

    with open(overrides_module.__file__, encoding='utf-8') as handle:
        tree = ast.parse(handle.read())

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    roots = {name.split('.')[0] for name in imported}
    assert 'fastapi' not in roots
    assert 'aiogram' not in roots
