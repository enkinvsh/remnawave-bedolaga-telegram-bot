"""Фрагменты главного экрана, вынесенные из ``menu.py`` в локали.

Раньше эти строки были захардкожены в коде: редактор локалей их не показывал,
а значит владелец не мог поправить текст экрана целиком. Тест сторожит их
наличие во ВСЕХ языках и одинаковый набор ``{placeholder}``-ов — иначе
``.format()`` упадёт в рантайме на другом языке.
"""

import json
import re
from pathlib import Path

import pytest


LOCALE_DIR = Path(__file__).resolve().parents[1] / 'app' / 'localization' / 'locales'
LANGS = ['ru', 'en', 'ua', 'fa', 'zh']
PLACEHOLDER_RE = re.compile(r'\{[^}]*\}')

# key -> ожидаемый набор плейсхолдеров
MAIN_MENU_KEYS = {
    'MAIN_MENU_TARIFF_LINE': {'{tariff_name}'},
    'SUB_MULTI_FALLBACK_NAME': set(),
    'SUB_MULTI_SUFFIX_EXPIRED': set(),
    'SUB_MULTI_SUFFIX_DISABLED': set(),
    'SUB_MULTI_SUFFIX_LIMITED': set(),
    'SUB_MULTI_SUFFIX_UNTIL': {'{end_date}', '{days}'},
}


@pytest.fixture(scope='module')
def locales():
    return {lang: json.loads((LOCALE_DIR / f'{lang}.json').read_text(encoding='utf-8')) for lang in LANGS}


@pytest.mark.parametrize('key', sorted(MAIN_MENU_KEYS))
@pytest.mark.parametrize('lang', LANGS)
def test_main_menu_key_present_in_every_locale(locales, lang, key):
    assert key in locales[lang], f'{key} отсутствует в {lang}.json'
    assert isinstance(locales[lang][key], str)
    assert locales[lang][key], f'{key} в {lang}.json пустой'


@pytest.mark.parametrize('key', sorted(MAIN_MENU_KEYS))
@pytest.mark.parametrize('lang', LANGS)
def test_main_menu_key_placeholders(locales, lang, key):
    value = locales[lang].get(key, '')
    assert set(PLACEHOLDER_RE.findall(value)) == MAIN_MENU_KEYS[key]


@pytest.mark.parametrize('key', sorted(MAIN_MENU_KEYS))
def test_translations_are_not_copies_of_russian(locales, key):
    """Русский текст в en/zh — признак незаполненного перевода.

    Исключение: строки, состоящие только из плейсхолдеров/пунктуации, могут
    совпадать законно, поэтому смотрим на наличие кириллицы.
    """
    ru_value = locales['ru'][key]
    if not re.search(r'[а-яА-Я]', ru_value):
        pytest.skip('в русском значении нет кириллицы — сравнивать нечего')

    for lang in ('en', 'zh'):
        assert locales[lang][key] != ru_value, f'{lang}.json:{key} — не переведён (копия ru)'
