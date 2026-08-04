"""Характеризационный тест кнопки «Подключиться» в текущем (легаси) главном меню.

ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ.
Форму этой кнопки выбирает настройка `CONNECT_BUTTON_MODE`: пять веток, у трёх из
них есть откат на callback при пустой ссылке подписки. Логику потребовалось отдать
конструктору меню, а копировать пять веток — гарантированный дрейф. Значит их надо
вынести в одну функцию, и перед выносом зафиксировать, что именно рендерит легаси
СЕЙЧАС: тест написан ДО извлечения, проходил на неизменённом коде и обязан
проходить после — побайтово тот же результат.

В VPN-биллинге «Подключиться» — главная кнопка бота. Подмена Mini App на обычный
callback ломает подключение всем платящим подписчикам сразу, поэтому здесь
проверяется не «кнопка есть», а конкретное поле: `web_app.url` / `url` /
`callback_data`.
"""

import pytest

from app.config import settings
from app.keyboards.inline import get_main_menu_keyboard


SUBSCRIPTION_URL = 'https://panel.example.com/sub/abcdef'
CRYPTO_LINK = 'happ://crypto/abcdef'
CUSTOM_MINIAPP_URL = 'https://miniapp.example.com/app'


class _FakeSubscription:
    def __init__(self, *, subscription_url: str = '', subscription_crypto_link: str = '') -> None:
        self.subscription_url = subscription_url
        self.subscription_crypto_link = subscription_crypto_link
        self.is_trial = False
        self.traffic_limit_gb = 0
        self.traffic_used_gb = 0.0
        self.days_left = 30
        self.autopay_enabled = False
        self.is_active = True


def _connect_button(subscription):
    markup = get_main_menu_keyboard(
        language='ru',
        has_active_subscription=True,
        subscription_is_active=True,
        subscription=subscription,
    )
    return markup.inline_keyboard[0][0]


def _shape(button) -> tuple[str | None, str | None, str | None]:
    """Форма кнопки: (web_app.url, url, callback_data) — ровно то, что ломается."""
    return (button.web_app.url if button.web_app else None, button.url, button.callback_data)


# (id, CONNECT_BUTTON_MODE, есть ли ссылка, ожидаемая форма)
CONNECT_CASES = [
    pytest.param(
        'miniapp_subscription',
        True,
        (SUBSCRIPTION_URL, None, None),
        id='miniapp_subscription-with-link',
    ),
    pytest.param(
        'miniapp_subscription',
        False,
        (None, None, 'subscription_connect'),
        id='miniapp_subscription-fallback',
    ),
    pytest.param(
        'miniapp_custom',
        True,
        (CUSTOM_MINIAPP_URL, None, None),
        id='miniapp_custom-with-link',
    ),
    pytest.param(
        'miniapp_custom',
        False,
        (CUSTOM_MINIAPP_URL, None, None),
        id='miniapp_custom-ignores-missing-link',
    ),
    pytest.param('link', True, (None, SUBSCRIPTION_URL, None), id='link-with-link'),
    pytest.param('link', False, (None, None, 'subscription_connect'), id='link-fallback'),
    pytest.param(
        'happ_cryptolink',
        True,
        (None, None, 'open_subscription_link'),
        id='happ_cryptolink-with-link',
    ),
    pytest.param(
        'happ_cryptolink',
        False,
        (None, None, 'subscription_connect'),
        id='happ_cryptolink-fallback',
    ),
    pytest.param('unknown_mode', True, (None, None, 'subscription_connect'), id='unknown-mode-fallback'),
]


@pytest.mark.parametrize(('mode', 'has_link', 'expected'), CONNECT_CASES)
def test_legacy_connect_button_shape(monkeypatch, mode: str, has_link: bool, expected) -> None:
    """Форма кнопки «Подключиться» в текущем меню зафиксирована по каждому режиму."""
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', mode)
    monkeypatch.setattr(settings, 'MINIAPP_CUSTOM_URL', CUSTOM_MINIAPP_URL)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', False)

    subscription = _FakeSubscription(
        subscription_url=SUBSCRIPTION_URL if has_link else '',
        subscription_crypto_link=SUBSCRIPTION_URL if has_link else '',
    )

    assert _shape(_connect_button(subscription)) == expected


def test_legacy_connect_button_keeps_locale_label(monkeypatch) -> None:
    """Подпись кнопки берётся из локали, а не из настройки режима подключения."""
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'link')

    button = _connect_button(_FakeSubscription(subscription_url=SUBSCRIPTION_URL))

    assert button.text == '🔗 Подключиться'


def test_legacy_happ_cryptolink_prefers_crypto_link(monkeypatch) -> None:
    """В режиме happ_cryptolink ссылка берётся из `subscription_crypto_link`."""
    monkeypatch.setattr(settings, 'CONNECT_BUTTON_MODE', 'happ_cryptolink')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')

    subscription = _FakeSubscription(subscription_url='', subscription_crypto_link=CRYPTO_LINK)

    assert _shape(_connect_button(subscription)) == (None, None, 'subscription_connect')
