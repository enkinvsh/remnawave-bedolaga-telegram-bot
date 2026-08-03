"""Строки экрана «Информация о подписке», вынесенные из ``pricing.py``.

Раньше они были захардкожены в коде: редактор локалей их не показывал, и экран
нельзя было отредактировать целиком. Тест сторожит наличие ключей во ВСЕХ
языках и одинаковый набор ``{placeholder}``-ов — иначе ``.format()`` упадёт в
рантайме на другом языке.
"""

import json
import re
from pathlib import Path

import pytest


LOCALE_DIR = Path(__file__).resolve().parents[1] / 'app' / 'localization' / 'locales'
LANGS = ['ru', 'en', 'ua', 'fa', 'zh']
PLACEHOLDER_RE = re.compile(r'\{[^}]*\}')

SUBSCRIPTION_INFO_KEYS = {
    'SUBSCRIPTION_INFO_STATUS_TRIAL': set(),
    'SUBSCRIPTION_INFO_STATUS_PAID': set(),
    'SUBSCRIPTION_INFO_STATUS_EXPIRED': set(),
    'SUBSCRIPTION_INFO_TYPE_TRIAL': set(),
    'SUBSCRIPTION_INFO_TYPE_PAID': set(),
    'SUBSCRIPTION_INFO_TRAFFIC_UNLIMITED': set(),
    'SUBSCRIPTION_INFO_TRAFFIC_LIMITED': {'{traffic_gb}'},
    'SUBSCRIPTION_INFO_AUTOPAY_ON': set(),
    'SUBSCRIPTION_INFO_AUTOPAY_OFF': set(),
    'SUBSCRIPTION_INFO_MONTHLY_COST': {'{price}'},
    'SUBSCRIPTION_INFO_PURCHASED_TRAFFIC_TITLE': set(),
    'SUBSCRIPTION_INFO_PURCHASED_TRAFFIC_ITEM': {'{traffic_gb}', '{time_text}'},
    'SUBSCRIPTION_INFO_PURCHASED_TRAFFIC_PROGRESS': {'{bar}', '{percent}', '{expire_date}'},
    'SUBSCRIPTION_INFO_PURCHASED_EXPIRES_TODAY': set(),
    'SUBSCRIPTION_INFO_PURCHASED_ONE_DAY_LEFT': set(),
    'SUBSCRIPTION_INFO_PURCHASED_FEW_DAYS_LEFT': {'{days}'},
    'SUBSCRIPTION_INFO_PURCHASED_MANY_DAYS_LEFT': {'{days}'},
    'SUBSCRIPTION_INFO_IMPORT_LINK': {'{url}'},
}


@pytest.fixture(scope='module')
def locales():
    return {lang: json.loads((LOCALE_DIR / f'{lang}.json').read_text(encoding='utf-8')) for lang in LANGS}


@pytest.mark.parametrize('key', sorted(SUBSCRIPTION_INFO_KEYS))
@pytest.mark.parametrize('lang', LANGS)
def test_key_present_in_every_locale(locales, lang, key):
    assert key in locales[lang], f'{key} отсутствует в {lang}.json'
    assert isinstance(locales[lang][key], str)
    assert locales[lang][key], f'{key} в {lang}.json пустой'


@pytest.mark.parametrize('key', sorted(SUBSCRIPTION_INFO_KEYS))
@pytest.mark.parametrize('lang', LANGS)
def test_key_placeholders(locales, lang, key):
    value = locales[lang].get(key, '')
    assert set(PLACEHOLDER_RE.findall(value)) == SUBSCRIPTION_INFO_KEYS[key]


@pytest.mark.parametrize('key', sorted(SUBSCRIPTION_INFO_KEYS))
def test_translations_are_not_copies_of_russian(locales, key):
    ru_value = locales['ru'][key]
    if not re.search(r'[а-яА-Я]', ru_value):
        pytest.skip('в русском значении нет кириллицы — сравнивать нечего')

    for lang in ('en', 'zh'):
        assert locales[lang][key] != ru_value, f'{lang}.json:{key} — не переведён (копия ru)'
