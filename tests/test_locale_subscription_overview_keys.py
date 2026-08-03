"""Строки экрана «Подписка» (кнопка menu_subscription), вынесенные из purchase.py.

Это тот экран, который пользователь реально открывает; раньше часть его строк
была захардкожена в обработчике, и редактор локалей их не показывал.
"""

import json
import re
from pathlib import Path

import pytest


LOCALE_DIR = Path(__file__).resolve().parents[1] / 'app' / 'localization' / 'locales'
LANGS = ['ru', 'en', 'ua', 'fa', 'zh']
PLACEHOLDER_RE = re.compile(r'\{[^}]*\}')

OVERVIEW_KEYS = {
    'SUBSCRIPTION_PURCHASED_EXPIRES_TODAY': set(),
    'SUBSCRIPTION_PURCHASED_ONE_DAY_LEFT': set(),
    'SUBSCRIPTION_PURCHASED_FEW_DAYS_LEFT': {'{days}'},
    'SUBSCRIPTION_PURCHASED_MANY_DAYS_LEFT': {'{days}'},
    'SUBSCRIPTION_PURCHASED_TRAFFIC_ITEM': {'{traffic_gb}', '{time_text}'},
    'SUBSCRIPTION_PURCHASED_TRAFFIC_PROGRESS': {'{bar}', '{percent}', '{expire_date}'},
    'SUBSCRIPTION_DEVICE_UNKNOWN': set(),
    'SUBSCRIPTION_DEVICE_LINE': {'{platform}', '{model}'},
    'SUBSCRIPTION_DEVICE_ITEM': {'{device}'},
    'SUBSCRIPTION_TARIFF_NAME_LINE': {'{tariff_name}'},
    'SUBSCRIPTION_TARIFF_TYPE_DAILY': set(),
    'SUBSCRIPTION_TARIFF_TYPE_PERIODIC': set(),
    'SUBSCRIPTION_TARIFF_TYPE_LINE': {'{tariff_type}'},
    'SUBSCRIPTION_TARIFF_TRAFFIC_LINE': {'{traffic_gb}'},
    'SUBSCRIPTION_TARIFF_TRAFFIC_UNLIMITED_LINE': set(),
    'SUBSCRIPTION_TARIFF_DEVICES_LINE': {'{device_limit}'},
    'SUBSCRIPTION_TARIFF_DAILY_PRICE_LINE': {'{price}'},
    'SUBSCRIPTION_TARIFF_DAILY_PAUSED': set(),
    'SUBSCRIPTION_TARIFF_DAILY_TIME_LEFT': {'{hours}', '{minutes}'},
    'SUBSCRIPTION_TARIFF_DAILY_CHARGE_PAUSED': set(),
    'SUBSCRIPTION_TARIFF_DAILY_NEXT_CHARGE': {'{hours}', '{minutes}'},
    'SUBSCRIPTION_TARIFF_DAILY_PROGRESS': {'{bar}', '{percent}'},
    'SUBSCRIPTION_TARIFF_DAILY_FIRST_CHARGE': set(),
}


@pytest.fixture(scope='module')
def locales():
    return {lang: json.loads((LOCALE_DIR / f'{lang}.json').read_text(encoding='utf-8')) for lang in LANGS}


@pytest.mark.parametrize('key', sorted(OVERVIEW_KEYS))
@pytest.mark.parametrize('lang', LANGS)
def test_key_present_in_every_locale(locales, lang, key):
    assert key in locales[lang], f'{key} отсутствует в {lang}.json'
    assert isinstance(locales[lang][key], str)
    assert locales[lang][key], f'{key} в {lang}.json пустой'


@pytest.mark.parametrize('key', sorted(OVERVIEW_KEYS))
@pytest.mark.parametrize('lang', LANGS)
def test_key_placeholders(locales, lang, key):
    value = locales[lang].get(key, '')
    assert set(PLACEHOLDER_RE.findall(value)) == OVERVIEW_KEYS[key]


@pytest.mark.parametrize('key', sorted(OVERVIEW_KEYS))
def test_translations_are_not_copies_of_russian(locales, key):
    ru_value = locales['ru'][key]
    if not re.search(r'[а-яА-Я]', ru_value):
        pytest.skip('в русском значении нет кириллицы — сравнивать нечего')

    for lang in ('en', 'zh'):
        assert locales[lang][key] != ru_value, f'{lang}.json:{key} — не переведён (копия ru)'
