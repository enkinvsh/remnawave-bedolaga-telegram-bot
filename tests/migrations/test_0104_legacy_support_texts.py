"""Разбор legacy-хранилища текста поддержки в миграции 0104.

Миграция переносит ``support_info_texts`` из ``data/support_settings.json`` в
таблицу ``locale_overrides``. Файл пишется людьми и живёт на проде годами, так
что в нём бывает мусор: пустые строки, ``null``, число вместо текста, код языка
с регионом. Разбор вынесен в чистую функцию — её и проверяем, без БД и без
Alembic.
"""

import importlib.util
from pathlib import Path


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / 'migrations'
    / 'alembic'
    / 'versions'
    / '0104_support_info_to_locale_overrides.py'
)


def _load_legacy_support_texts():
    spec = importlib.util.spec_from_file_location('migration_0104', _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._legacy_support_texts


def test_normal_two_language_dict():
    parse = _load_legacy_support_texts()

    assert parse({'ru': 'Текст RU', 'en': 'Text EN'}) == [('ru', 'Текст RU'), ('en', 'Text EN')]


def test_none_yields_nothing():
    assert _load_legacy_support_texts()(None) == []


def test_non_dict_yields_nothing():
    parse = _load_legacy_support_texts()

    assert parse([]) == []
    assert parse('x') == []


def test_blank_and_non_string_values_are_skipped():
    parse = _load_legacy_support_texts()

    raw = {'ru': 'Текст', 'en': '', 'ua': '   ', 'fa': None, 'zh': 42}

    assert parse(raw) == [('ru', 'Текст')]


def test_regional_language_code_is_normalized():
    """Legacy-геттер нормализовал язык как ``lang.split('-')[0].lower()``."""
    assert _load_legacy_support_texts()({'ru-RU': 'Текст'}) == [('ru', 'Текст')]
