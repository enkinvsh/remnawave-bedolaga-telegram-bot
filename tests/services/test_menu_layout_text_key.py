"""Откуда конструктор меню берёт подпись кнопки.

Паритетный тест (`tests/keyboards/test_main_menu_layout_parity.py`) сторожит итог —
что меню с флагом и без него выглядит одинаково. Здесь заперты сами правила, которые
на паритете не видны: точечная правка одного языка, кастомные кнопки и круг
«сохранил -> прочитал» через схему.
"""

import copy
from typing import Any

import pytest

from app.localization.texts import get_texts
from app.services.menu_layout.constants import DEFAULT_MENU_CONFIG
from app.services.menu_layout.schemas import MenuButtonConfig
from app.services.menu_layout.service import MenuLayoutService


def _builtin(button_id: str) -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_MENU_CONFIG['buttons'][button_id])


def _resolve(button: dict[str, Any], language: str) -> str:
    return MenuLayoutService._resolve_button_text(button, language, get_texts(language))


@pytest.mark.parametrize(
    ('language', 'expected'),
    [
        ('ru', '🧪 Тестовая подписка'),
        ('en', '🎁 Trial subscription'),
        ('fa', '🧪 اشتراک آزمایشی'),
        ('zh', '🧪试用订阅'),
        ('ua', '🧪 Тестова підписка'),
    ],
)
def test_builtin_label_comes_from_locale(language: str, expected: str) -> None:
    """Встроенная кнопка подписана локалью пользователя, а не словарём конфигурации."""
    assert _resolve(_builtin('trial'), language) == expected


def test_every_builtin_with_key_ships_without_literal_text() -> None:
    """Литерал у встроенной кнопки означал бы «админ так решил» — по умолчанию его нет."""
    for button_id, button in DEFAULT_MENU_CONFIG['buttons'].items():
        if button.get('text_key'):
            assert button['text'] == {}, button_id


def test_admin_override_beats_locale_only_in_its_own_language() -> None:
    """Правка подписи из конструктора перебивает локаль, но лишь для своего языка."""
    button = _builtin('trial')
    button['text'] = {'ru': '🔥 Мой триал'}

    assert _resolve(button, 'ru') == '🔥 Мой триал'
    assert _resolve(button, 'en') == get_texts('en').MENU_TRIAL
    assert _resolve(button, 'fa') == get_texts('fa').MENU_TRIAL


def test_blank_override_returns_the_button_to_the_locale() -> None:
    """Стерев текст в редакторе, админ возвращает подпись локали, а не пустую кнопку."""
    button = _builtin('trial')
    button['text'] = {'ru': '   '}

    assert _resolve(button, 'ru') == get_texts('ru').MENU_TRIAL


def test_custom_button_keeps_its_own_text() -> None:
    """У кнопки тенанта ключа локализации нет: её словарь остаётся авторитетным."""
    button = {
        'type': 'callback',
        'text': {'ru': '🎉 Акция', 'en': '🎉 Promo'},
        'action': 'promo_action',
    }

    assert _resolve(button, 'ru') == '🎉 Акция'
    assert _resolve(button, 'fa') == '🎉 Promo'


def test_round_trip_through_schema_keeps_the_locale_key() -> None:
    """Потеря text_key на круге кабинета оставила бы встроенные кнопки без подписи."""
    for button_id, button in DEFAULT_MENU_CONFIG['buttons'].items():
        dumped = MenuButtonConfig.model_validate(button).model_dump(mode='json')
        assert dumped['text_key'] == button['text_key'], button_id


def test_balance_placeholder_is_found_through_the_locale_key() -> None:
    """Плейсхолдер `{balance}` живёт теперь в локали — автоопределение обязано смотреть туда."""
    balance = _builtin('balance')

    assert MenuLayoutService._text_has_placeholders(balance['text'], balance['text_key']) is True
    assert MenuLayoutService._text_has_placeholders(balance['text']) is False
