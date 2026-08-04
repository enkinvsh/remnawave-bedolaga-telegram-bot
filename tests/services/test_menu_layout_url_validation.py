"""Тесты защиты от произвольных ссылок в конструкторе меню бота.

Проверка живёт в `MenuLayoutService.validate_config` — общей точке входа и для
кабинета (`app/cabinet/routes/admin_bot_menu.py`), и для админского REST API
(`app/webapi/routes/menu_layout.py`). Тесты бьют именно по сервису, чтобы
зафиксировать: защита одна на обе поверхности.

Угроза: кнопка меню с `type=url` кладёт произвольную ссылку в главное меню бота,
через который люди платят деньги, — готовая фишинговая площадка.
"""

from __future__ import annotations

import pytest

from app.services.menu_layout.service import MenuLayoutService


def _config_with_url_button(action: str) -> dict:
    """Минимальная валидная конфигурация с одной url-кнопкой."""
    return {
        'rows': [{'id': 'row_links', 'buttons': ['btn_link'], 'max_per_row': 1}],
        'buttons': {
            'btn_link': {
                'type': 'url',
                'text': {'ru': 'Ссылка'},
                'action': action,
                'enabled': True,
            }
        },
    }


def _config_with_webapp_url(webapp_url: str) -> dict:
    """Конфигурация с встроенной кнопкой, открывающей Mini App напрямую."""
    return {
        'rows': [{'id': 'row_app', 'buttons': ['btn_app'], 'max_per_row': 1}],
        'buttons': {
            'btn_app': {
                'type': 'builtin',
                'builtin_id': 'connect',
                'text': {'ru': 'Подключиться'},
                'action': 'subscription_connect',
                'open_mode': 'direct',
                'webapp_url': webapp_url,
                'enabled': True,
            }
        },
    }


def _config_with_mini_app_action(action: str) -> dict:
    """`type=mini_app` кладёт `action` прямо в `WebAppInfo(url=...)` — тот же сток."""
    return {
        'rows': [{'id': 'row_app', 'buttons': ['btn_app'], 'max_per_row': 1}],
        'buttons': {
            'btn_app': {
                'type': 'mini_app',
                'text': {'ru': 'Приложение'},
                'action': action,
                'enabled': True,
            }
        },
    }


def _url_errors(result: dict, field: str) -> list[dict]:
    return [error for error in result['errors'] if error['field'] == field]


# ---- 1. Схема: https проходит, http нет --------------------------------------


def test_https_url_button_is_accepted():
    result = MenuLayoutService.validate_config(_config_with_url_button('https://example.com/promo'))

    assert result['is_valid'] is True
    assert result['errors'] == []


def test_plain_http_url_button_is_rejected():
    result = MenuLayoutService.validate_config(_config_with_url_button('http://evil.com'))

    assert result['is_valid'] is False
    assert _url_errors(result, 'buttons.btn_link.action')


# ---- 2. Опасные схемы --------------------------------------------------------


@pytest.mark.parametrize(
    'action',
    [
        'javascript:alert(1)',
        'JavaScript:alert(1)',
        'data:text/html,<script>alert(1)</script>',
        'file:///etc/passwd',
        'vbscript:msgbox(1)',
        '//evil.com/phishing',
        'example.com',
    ],
)
def test_dangerous_schemes_are_rejected(action: str):
    result = MenuLayoutService.validate_config(_config_with_url_button(action))

    assert result['is_valid'] is False, f'{action!r} прошёл валидацию'
    assert _url_errors(result, 'buttons.btn_link.action')


@pytest.mark.parametrize('action', ['javascript:alert(1)', 'data:text/html,<b>x', 'file:///etc/passwd'])
def test_dangerous_schemes_are_rejected_in_every_sink(action: str):
    """Опасные схемы не проходят ни в один сток — послабление для tg их не задело."""
    for config, field in (
        (_config_with_url_button(action), 'buttons.btn_link.action'),
        (_config_with_webapp_url(action), 'buttons.btn_app.webapp_url'),
        (_config_with_mini_app_action(action), 'buttons.btn_app.action'),
    ):
        result = MenuLayoutService.validate_config(config)
        assert result['is_valid'] is False, f'{action!r} прошёл валидацию в {field}'
        assert _url_errors(result, field)


def test_plain_http_is_rejected_in_every_sink():
    for config, field in (
        (_config_with_url_button('http://evil.com'), 'buttons.btn_link.action'),
        (_config_with_webapp_url('http://app.example.com'), 'buttons.btn_app.webapp_url'),
        (_config_with_mini_app_action('http://app.example.com'), 'buttons.btn_app.action'),
    ):
        result = MenuLayoutService.validate_config(config)
        assert result['is_valid'] is False, f'http прошёл валидацию в {field}'
        assert _url_errors(result, field)


# ---- 2a. tg:// — только для кнопок type=url ----------------------------------


@pytest.mark.parametrize(
    'action',
    [
        'tg://resolve?domain=dropweb_support',
        'tg://user?id=123',
        'tg://settings',
        'tg://settings/devices',
        'tg://addemoji?set=zenbot',
        'TG://user?id=123',
    ],
)
def test_tg_deep_link_is_accepted_on_url_button(action: str):
    """Bot API описывает `InlineKeyboardButton.url` как «HTTP or tg:// URL».

    У `tg://user?id=<numeric>` https-аналога нет вообще: t.me требует username,
    поэтому запрет отнимал возможность, а не риск.
    """
    result = MenuLayoutService.validate_config(_config_with_url_button(action))

    assert result['is_valid'] is True, f'{action!r} ошибочно отклонён'
    assert _url_errors(result, 'buttons.btn_link.action') == []


@pytest.mark.parametrize('deep_link', ['tg://resolve?domain=dropweb_support', 'tg://user?id=123', 'tg://settings'])
def test_tg_deep_link_is_rejected_for_webapp_url(deep_link: str):
    """`WebAppInfo.url` — «An HTTPS URL of a Web App», tg:// туда не годится."""
    result = MenuLayoutService.validate_config(_config_with_webapp_url(deep_link))

    assert result['is_valid'] is False, f'{deep_link!r} прошёл валидацию'
    assert _url_errors(result, 'buttons.btn_app.webapp_url')


@pytest.mark.parametrize('deep_link', ['tg://resolve?domain=dropweb_support', 'tg://user?id=123', 'tg://settings'])
def test_tg_deep_link_is_rejected_for_mini_app_action(deep_link: str):
    result = MenuLayoutService.validate_config(_config_with_mini_app_action(deep_link))

    assert result['is_valid'] is False, f'{deep_link!r} прошёл валидацию'
    assert _url_errors(result, 'buttons.btn_app.action')


@pytest.mark.parametrize(
    'action',
    [
        'tg://',  # действие не указано
        'tg://?id=123',  # только параметры, без действия
        'tg:settings',  # форма без `//` — Bot API документирует именно `tg://`
        'tg://user:pass@resolve',  # `@` в deep link не бывает
    ],
)
def test_malformed_tg_deep_link_is_rejected(action: str):
    result = MenuLayoutService.validate_config(_config_with_url_button(action))

    assert result['is_valid'] is False, f'{action!r} прошёл валидацию'
    assert _url_errors(result, 'buttons.btn_link.action')


def test_tg_deep_link_is_not_judged_as_a_host():
    """`urlsplit('tg://user?id=5').hostname` == 'user' — это имя действия, не хост.

    Проверки «голый IP» и «домен обязателен» к нему неприменимы: `tg://user?id=5`
    обязан пройти, хотя доменом там и не пахнет.
    """
    result = MenuLayoutService.validate_config(_config_with_url_button('tg://user?id=5'))

    assert result['is_valid'] is True
    assert result['errors'] == []


# ---- 3. Хост: пустой, IP-литерал, встроенные учётные данные -------------------


@pytest.mark.parametrize(
    'action',
    [
        'https://user:pass@example.com',  # классический спуфинг адресной строки
        'https://sberbank.ru@evil.com/login',
        'https:///no-host',  # пустой хост
        'https://',
        'https://93.184.216.34/promo',  # голый IPv4
        'https://[2606:2800:220:1:248:1893:25c8:1946]/promo',  # голый IPv6
    ],
)
def test_hostile_hosts_are_rejected(action: str):
    result = MenuLayoutService.validate_config(_config_with_url_button(action))

    assert result['is_valid'] is False, f'{action!r} прошёл валидацию'
    assert _url_errors(result, 'buttons.btn_link.action')


def test_legitimate_own_domains_stay_allowed():
    """Белого списка доменов нет: владелец линкует свои сайты и поддержку свободно."""
    for action in ('https://dropweb.org', 'https://t.me/dropweb_support', 'https://sub.domain.example.co.uk/a?b=1'):
        result = MenuLayoutService.validate_config(_config_with_url_button(action))
        assert result['is_valid'] is True, f'{action!r} ошибочно отклонён'


# ---- 4. webapp_url подчиняется тем же правилам -------------------------------


def test_https_webapp_url_is_accepted():
    result = MenuLayoutService.validate_config(_config_with_webapp_url('https://app.example.com/miniapp'))

    assert result['is_valid'] is True


@pytest.mark.parametrize(
    'webapp_url',
    [
        'http://app.example.com',
        'javascript:alert(1)',
        'https://user:pass@app.example.com',
        'https://10.0.0.5/miniapp',
    ],
)
def test_bad_webapp_url_is_rejected(webapp_url: str):
    result = MenuLayoutService.validate_config(_config_with_webapp_url(webapp_url))

    assert result['is_valid'] is False, f'{webapp_url!r} прошёл валидацию'
    assert _url_errors(result, 'buttons.btn_app.webapp_url')


# ---- 4a. mini_app: тот же сток WebAppInfo, что и webapp_url -------------------


def test_mini_app_action_obeys_the_same_rule():
    result = MenuLayoutService.validate_config(_config_with_mini_app_action('javascript:alert(1)'))

    assert result['is_valid'] is False
    assert _url_errors(result, 'buttons.btn_app.action')


def test_https_mini_app_action_is_accepted():
    result = MenuLayoutService.validate_config(_config_with_mini_app_action('https://app.example.com/miniapp'))

    assert result['is_valid'] is True


# ---- 5. Обычные callback-меню не задеты --------------------------------------


def test_callback_only_config_is_untouched():
    config = {
        'rows': [{'id': 'row_main', 'buttons': ['btn_balance'], 'max_per_row': 1}],
        'buttons': {
            'btn_balance': {
                'type': 'builtin',
                'builtin_id': 'balance',
                'text': {'ru': 'Баланс'},
                'action': 'menu_balance',
                'enabled': True,
            }
        },
    }

    result = MenuLayoutService.validate_config(config)

    assert result['is_valid'] is True
    assert result['errors'] == []


def test_default_config_stays_valid():
    """Дефолтная раскладка не должна вдруг стать невалидной из-за нового правила."""
    result = MenuLayoutService.validate_config(MenuLayoutService.get_default_config())

    assert result['is_valid'] is True


def test_existing_validation_still_reports_dangling_button_reference():
    """Новая проверка не должна вытеснить старые."""
    result = MenuLayoutService.validate_config({'rows': [{'id': 'row_broken', 'buttons': ['ghost']}], 'buttons': {}})

    assert result['is_valid'] is False
    assert any(error['field'] == 'rows.row_broken.buttons' for error in result['errors'])


# ---- 6. Тот же барьер срабатывает на пути webapi ------------------------------


@pytest.mark.asyncio
async def test_webapi_validate_endpoint_rejects_hostile_url():
    """Доказательство общей точки: 33 ручки webapi получают защиту без правок."""
    from app.services.menu_layout.schemas import MenuButtonConfig, MenuLayoutValidateRequest, MenuRowConfig
    from app.webapi.routes.menu_layout import validate_menu_layout

    payload = MenuLayoutValidateRequest(
        rows=[MenuRowConfig(id='row_links', buttons=['btn_link'], max_per_row=1)],
        buttons={
            'btn_link': MenuButtonConfig(
                type='url',
                text={'ru': 'Ссылка'},
                action='javascript:alert(1)',
            )
        },
    )

    response = await validate_menu_layout(payload, _=None, db=None)

    assert response.is_valid is False
    assert any(error.field == 'buttons.btn_link.action' for error in response.errors)
