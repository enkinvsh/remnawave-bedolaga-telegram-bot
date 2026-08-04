"""Каталог встроенных кнопок (`/builtin-buttons`) — палитра конструктора меню.

Каталог — это то, ЧТО КОНСТРУКТОР ЗАПИСЫВАЕТ в конфигурацию тенанта, когда админ
добавляет встроенную кнопку из палитры. Поэтому у него две обязанности, и обе
проверяются здесь:

1. отдать `text_key` — без него кабинет физически не может завести кнопку
   «наследующую локаль», и вынужден вписать литерал, намертво прибив подпись,
   `locale_overrides` тенанта и все языки помимо ru/en;
2. показывать в `default_text` ТО, ЧТО БОТ РЕНДЕРИТ СЕЙЧАС, для каждого
   отгружаемого языка. Это превью палитры: если оно врёт, админ выбирает вслепую.

Правила разрешения подписи заперты рядом, в `test_menu_layout_text_key.py`.
"""

from typing import Any

import pytest

from app.config import settings
from app.localization.texts import get_texts
from app.services.menu_layout.constants import (
    BUILTIN_BUTTONS_INFO,
    CUSTOM_BUTTONS_SLOT_ID,
    DEFAULT_MENU_CONFIG,
)
from app.services.menu_layout.service import MenuLayoutService


def _catalogue() -> dict[str, dict[str, Any]]:
    return {item['id']: item for item in MenuLayoutService.get_builtin_buttons_info()}


@pytest.fixture
def locale_overrides():
    """Подставить override-ы тенанта и вернуть кеш в исходное состояние после теста."""
    from app.localization import overrides

    saved = overrides.get_override_cache()

    def apply(mapping: dict[tuple[str, str], str]) -> None:
        overrides.set_override_cache(mapping)

    yield apply
    overrides.set_override_cache(saved)


# ---- 1. text_key приходит в каталоге и совпадает с раскладкой по умолчанию ----


def test_catalogue_reports_the_same_text_key_as_the_default_layout() -> None:
    """Ключ в палитре и ключ в раскладке — один и тот же, иначе таблицы разъедутся."""
    catalogue = _catalogue()

    for button_id, button in DEFAULT_MENU_CONFIG['buttons'].items():
        assert button_id in catalogue, button_id
        assert catalogue[button_id]['text_key'] == button['text_key'], button_id


def test_every_builtin_with_a_locale_key_exposes_it() -> None:
    """Кнопка с ключом обязана отдать его наружу: без него кабинет пишет литерал."""
    catalogue = _catalogue()

    keyed = {button_id for button_id, button in DEFAULT_MENU_CONFIG['buttons'].items() if button.get('text_key')}
    assert keyed, 'в раскладке по умолчанию не осталось кнопок с ключом локали'

    for button_id in keyed:
        assert catalogue[button_id]['text_key'], button_id


# ---- 2. default_text честен: это то, что бот рисует сейчас --------------------


def test_subscription_preview_matches_the_live_menu() -> None:
    """Регресс: в каталоге годами лежало «📊 Подписка», а бот рисует «📱 Подписка»."""
    preview = _catalogue()['subscription']['default_text']

    assert preview['ru'] == get_texts('ru').MENU_SUBSCRIPTION
    assert preview['ru'] == '📱 Подписка'


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_preview_matches_the_locale_in_every_shipped_language(language: str) -> None:
    """Каталог покрывает ВСЕ отгружаемые языки, а не пару ru/en."""
    catalogue = _catalogue()

    for button_id, button in DEFAULT_MENU_CONFIG['buttons'].items():
        if not button.get('text_key'):
            continue
        expected = MenuLayoutService._resolve_button_text(button, language, get_texts(language))
        assert catalogue[button_id]['default_text'][language] == expected, (button_id, language)


def test_preview_covers_exactly_the_configured_languages() -> None:
    languages = set(settings.get_available_languages())

    for button_id, item in _catalogue().items():
        assert set(item['default_text']) == languages, button_id


def test_locale_override_moves_the_catalogue_preview(locale_overrides) -> None:
    """Тенант переписал подпись в редакторе локалей — палитра обязана показать ЕГО текст."""
    locale_overrides({('ru', 'MENU_SUBSCRIPTION'): '🛰️ Мой тариф'})

    assert _catalogue()['subscription']['default_text']['ru'] == '🛰️ Мой тариф'


def test_settings_owned_label_is_previewed_from_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """У `activate` подписи в локалях нет — её задаёт настройка бота, её и показываем."""
    monkeypatch.setattr(settings, 'ACTIVATE_BUTTON_TEXT', '🚀 Активировать', raising=False)

    preview = _catalogue()['activate']['default_text']
    assert preview['ru'] == '🚀 Активировать'
    assert preview['fa'] == '🚀 Активировать'


def test_keyless_builtins_preview_their_literal() -> None:
    """`resume_checkout` и `moderator_panel` эквивалента в локалях не имеют — литерал честен."""
    catalogue = _catalogue()

    for button_id in ('resume_checkout', 'moderator_panel'):
        literal = DEFAULT_MENU_CONFIG['buttons'][button_id]['text']
        assert catalogue[button_id]['default_text']['ru'] == literal['ru'], button_id
        assert catalogue[button_id]['default_text']['en'] == literal['en'], button_id


def test_custom_buttons_slot_is_labelled_as_a_slot() -> None:
    """Псевдо-кнопка своей подписи не имеет — в палитре нужен пояснительный текст, а не пустота."""
    preview = _catalogue()[CUSTOM_BUTTONS_SLOT_ID]['default_text']

    assert all(value.strip() for value in preview.values())


# ---- 3. Кнопка, заведённая так, как её теперь заводит кабинет -----------------


def _button_as_the_cabinet_adds_it(builtin_id: str) -> dict[str, Any]:
    """Ровно то, что пишет `addBuiltinButton` после починки: пустой text + ключ локали."""
    info = _catalogue()[builtin_id]
    return {
        'type': 'builtin',
        'builtin_id': info['id'],
        'text': {},
        'text_key': info['text_key'],
        'action': info['callback_data'],
        'enabled': True,
        'visibility': 'all',
        'conditions': info['default_conditions'],
        'dynamic_text': info['supports_dynamic_text'],
        'open_mode': 'callback',
    }


@pytest.mark.parametrize('language', ['ru', 'en', 'ua', 'zh', 'fa'])
def test_button_added_from_the_palette_renders_the_locale(language: str) -> None:
    button = _button_as_the_cabinet_adds_it('subscription')

    resolved = MenuLayoutService._resolve_button_text(button, language, get_texts(language))
    assert resolved == get_texts(language).MENU_SUBSCRIPTION


def test_locale_override_wins_for_a_button_added_from_the_palette(locale_overrides) -> None:
    """Главный смысл всей правки: override тенанта обязан доехать до кнопки из палитры."""
    locale_overrides({('ru', 'MENU_SUBSCRIPTION'): '🛰️ Мой тариф'})

    button = _button_as_the_cabinet_adds_it('subscription')
    assert MenuLayoutService._resolve_button_text(button, 'ru', get_texts('ru')) == '🛰️ Мой тариф'
    assert MenuLayoutService._resolve_button_text(button, 'en', get_texts('en')) == get_texts('en').MENU_SUBSCRIPTION


def test_balance_from_the_palette_keeps_placeholder_detection_on() -> None:
    """Подпись `balance` несёт `{balance}`: с `dynamic_text=False` юзер увидел бы сырой плейсхолдер."""
    assert _button_as_the_cabinet_adds_it('balance')['dynamic_text'] is True


# ---- 4. Устаревшего литерала в каталоге больше нет ----------------------------


def test_no_static_literal_can_shadow_a_locale_key() -> None:
    """Статическая таблица каталога не хранит подписи кнопок с ключом — расходиться нечему."""
    keyed = {button_id for button_id, button in DEFAULT_MENU_CONFIG['buttons'].items() if button.get('text_key')}

    for item in BUILTIN_BUTTONS_INFO:
        if item['id'] in keyed:
            assert not item.get('default_text'), item['id']


def test_catalogue_covers_the_default_layout_and_nothing_else() -> None:
    assert {item['id'] for item in BUILTIN_BUTTONS_INFO} == set(DEFAULT_MENU_CONFIG['buttons'])
