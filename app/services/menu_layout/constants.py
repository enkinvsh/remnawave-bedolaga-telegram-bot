"""Константы для конструктора меню.

ПОДПИСИ ВСТРОЕННЫХ КНОПОК ЖИВУТ В ЛОКАЛЯХ, А НЕ ЗДЕСЬ.
У каждой встроенной кнопки есть `text_key` — ровно тот ключ локализации, который
читает текущее (легаси) меню в `app/keyboards/inline.py::get_main_menu_keyboard`.
Поэтому `text` у таких кнопок ПУСТОЙ: непустой словарь означает «админ явно
переписал подпись в конструкторе» и перебивает локаль (см.
`MenuLayoutService._resolve_button_text`). Если бы здесь остались литералы,
конструктор считал бы их админской правкой и намертво прибивал бы: язык
пользователя (в конфигурации были только ru и en), `locale_overrides` тенанта из
редактора локалей и сами тексты локалей.

Кнопки без `text_key` — те, у которых в легаси-меню НЕТ эквивалента с ключом
локализации: `resume_checkout` (из главного меню убрана и живёт на экране
«Баланс») и `moderator_panel` (легаси хардкодит '🧑‍⚖️ Модерация' строкой).
Ключ им не выдуман: подпись остаётся литеральной, как у кастомных кнопок.
Отдельный случай — `activate`: её подпись живёт не в локалях, а в НАСТРОЙКЕ бота
`ACTIVATE_BUTTON_TEXT`, поэтому `text` у неё тоже пустой, а значение подставляет
`MenuLayoutService._resolve_button_text` (см. `_SETTINGS_TEXT_BUILTINS`).

РАСКЛАДКА ПО УМОЛЧАНИЮ ОБЯЗАНА СОВПАДАТЬ С ЛЕГАСИ-МЕНЮ ПОБАЙТОВО.
`get_main_menu_keyboard` кладёт в клавиатуру ровно три «прибитых» ряда — connect,
happ и баланс — а ВСЕ остальные кнопки сливает в ОДИН поток `paired_buttons` и
режет его по 2. Из-за этого пары «плывут»: спрятанная кнопка не оставляет дыру, а
подтягивает следующую. Семантические ряды (по одному на смысловую пару) такое
воспроизвести не могут в принципе, поэтому здесь тот же самый поток — одна строка
`main_row` со всеми непривязанными кнопками и `max_per_row=2`. `build_keyboard`
режет по `max_per_row` уже ВИДИМЫЕ кнопки строки, так что переток получается сам.
Строка остаётся обычной `MenuRowConfig`: админ видит её в конструкторе и может
вытащить любую кнопку в собственный ряд — но это будет его осознанное решение, а
не побочный эффект включения флага.

Условия, которые раньше висели на однокнопочных рядах (`simple_subscription_enabled`,
`contests_visible`, `language_selection_enabled`), переехали на сами кнопки: ряд
теперь общий, и условие на нём спрятало бы весь поток.
"""

from typing import Any


# Ключ для хранения конфигурации в SystemSetting
MENU_LAYOUT_CONFIG_KEY = 'menu_layout_config'

# Псевдо-кнопка: место в потоке, куда `build_keyboard` вставляет кнопки, переданные
# вызывающим в `MenuContext.custom_buttons`. Своей подписи и действия у неё нет —
# она разворачивается в ноль или больше готовых `InlineKeyboardButton`. В легаси-меню
# эти кнопки попадают в поток между «простой подпиской» и промокодом, поэтому место
# в раскладке значимо и должно быть перемещаемым, а не захардкоженным.
CUSTOM_BUTTONS_SLOT_ID = 'custom_buttons'

# Дефолтная конфигурация меню
DEFAULT_MENU_CONFIG: dict[str, Any] = {
    'version': 1,
    'rows': [
        {
            'id': 'connect_row',
            'buttons': ['connect'],
            'conditions': {'has_active_subscription': True, 'subscription_is_active': True},
            'max_per_row': 1,
        },
        {
            'id': 'happ_row',
            'buttons': ['happ_download'],
            'conditions': {
                'has_active_subscription': True,
                'subscription_is_active': True,
                'happ_enabled': True,
            },
            'max_per_row': 1,
        },
        {
            'id': 'balance_row',
            'buttons': ['balance'],
            'conditions': None,
            'max_per_row': 1,
        },
        {
            # Общий поток легаси-меню: порядок кнопок здесь — это порядок, в котором
            # `get_main_menu_keyboard` наполняет `paired_buttons`.
            'id': 'main_row',
            'buttons': [
                'subscription',
                'buy_traffic',
                'trial',
                'buy_subscription',
                'simple_subscription',
                CUSTOM_BUTTONS_SLOT_ID,
                'promocode',
                'referrals',
                'contests',
                'support',
                'activate',
                'info',
                'language',
            ],
            'conditions': None,
            'max_per_row': 2,
        },
        {
            'id': 'admin_row',
            'buttons': ['admin_panel'],
            'conditions': {'is_admin': True},
            'max_per_row': 1,
        },
        {
            'id': 'moderator_row',
            'buttons': ['moderator_panel'],
            'conditions': {'is_moderator': True},
            'max_per_row': 1,
        },
    ],
    'buttons': {
        'connect': {
            'type': 'builtin',
            'builtin_id': 'connect',
            'text': {},
            'text_key': 'CONNECT_BUTTON',
            'action': 'subscription_connect',
            'enabled': True,
            'visibility': 'subscribers',
            'conditions': {'has_active_subscription': True, 'subscription_is_active': True},
            'dynamic_text': False,
            'open_mode': 'callback',  # "callback" или "direct"
            'webapp_url': None,  # URL для Mini App при open_mode="direct"
        },
        'happ_download': {
            'type': 'builtin',
            'builtin_id': 'happ_download',
            'text': {},
            'text_key': 'HAPP_DOWNLOAD_BUTTON',
            'action': 'subscription_happ_download',
            'enabled': True,
            'visibility': 'subscribers',
            'conditions': None,
            'dynamic_text': False,
        },
        'subscription': {
            'type': 'builtin',
            'builtin_id': 'subscription',
            'text': {},
            'text_key': 'MENU_SUBSCRIPTION',
            'action': 'menu_subscription',
            'enabled': True,
            'visibility': 'subscribers',
            'conditions': None,
            'dynamic_text': False,
        },
        'buy_traffic': {
            'type': 'builtin',
            'builtin_id': 'buy_traffic',
            'text': {},
            'text_key': 'BUY_TRAFFIC_BUTTON',
            'action': 'buy_traffic',
            'enabled': True,
            'visibility': 'subscribers',
            'conditions': {'has_traffic_limit': True, 'traffic_topup_enabled': True},
            'dynamic_text': False,
        },
        'balance': {
            'type': 'builtin',
            'builtin_id': 'balance',
            'text': {},
            'text_key': 'BALANCE_BUTTON',
            'action': 'menu_balance',
            'enabled': True,
            'visibility': 'all',
            'conditions': None,
            'dynamic_text': True,
        },
        'trial': {
            'type': 'builtin',
            'builtin_id': 'trial',
            'text': {},
            'text_key': 'MENU_TRIAL',
            'action': 'menu_trial',
            'enabled': True,
            'visibility': 'all',
            'conditions': {'show_trial': True},
            'dynamic_text': False,
        },
        'buy_subscription': {
            'type': 'builtin',
            'builtin_id': 'buy_subscription',
            'text': {},
            'text_key': 'MENU_BUY_SUBSCRIPTION',
            'action': 'menu_buy',
            'enabled': True,
            'visibility': 'all',
            'conditions': {'show_buy': True},
            'dynamic_text': False,
        },
        'simple_subscription': {
            'type': 'builtin',
            'builtin_id': 'simple_subscription',
            'text': {},
            'text_key': 'MENU_SIMPLE_SUBSCRIPTION',
            'action': 'simple_subscription_purchase',
            'enabled': True,
            'visibility': 'all',
            'conditions': {'simple_subscription_enabled': True},
            'dynamic_text': False,
        },
        CUSTOM_BUTTONS_SLOT_ID: {
            'type': 'builtin',
            'builtin_id': CUSTOM_BUTTONS_SLOT_ID,
            'text': {},
            'text_key': None,
            'action': '',
            'enabled': True,
            'visibility': 'all',
            'conditions': None,
            'dynamic_text': False,
        },
        'resume_checkout': {
            'type': 'builtin',
            'builtin_id': 'resume_checkout',
            # Ключа локализации нет: в легаси-меню этой кнопки больше нет вообще.
            'text': {'ru': '↩️ Вернуться к оформлению', 'en': '↩️ Resume checkout'},
            'text_key': None,
            'action': 'return_to_saved_cart',
            'enabled': True,
            'visibility': 'all',
            'conditions': None,
            'dynamic_text': False,
        },
        'promocode': {
            'type': 'builtin',
            'builtin_id': 'promocode',
            'text': {},
            'text_key': 'MENU_PROMOCODE',
            'action': 'menu_promocode',
            'enabled': True,
            'visibility': 'all',
            'conditions': None,
            'dynamic_text': False,
        },
        'referrals': {
            'type': 'builtin',
            'builtin_id': 'referrals',
            'text': {},
            'text_key': 'MENU_REFERRALS',
            'action': 'menu_referrals',
            'enabled': True,
            'visibility': 'all',
            'conditions': {'referral_enabled': True},
            'dynamic_text': False,
        },
        'contests': {
            'type': 'builtin',
            'builtin_id': 'contests',
            'text': {},
            'text_key': 'CONTESTS_BUTTON',
            'action': 'contests_menu',
            'enabled': True,
            'visibility': 'all',
            'conditions': {'contests_visible': True},
            'dynamic_text': False,
        },
        'support': {
            'type': 'builtin',
            'builtin_id': 'support',
            'text': {},
            'text_key': 'MENU_SUPPORT',
            'action': 'menu_support',
            'enabled': True,
            'visibility': 'all',
            'conditions': {'support_enabled': True},
            'dynamic_text': False,
        },
        'activate': {
            'type': 'builtin',
            'builtin_id': 'activate',
            # Подпись живёт в настройке бота ACTIVATE_BUTTON_TEXT, а не в локалях.
            'text': {},
            'text_key': None,
            'action': 'activate_button',
            'enabled': True,
            'visibility': 'all',
            'conditions': {'activate_button_visible': True},
            'dynamic_text': False,
        },
        'info': {
            'type': 'builtin',
            'builtin_id': 'info',
            'text': {},
            'text_key': 'MENU_INFO',
            'action': 'menu_info',
            'enabled': True,
            'visibility': 'all',
            'conditions': None,
            'dynamic_text': False,
        },
        'language': {
            'type': 'builtin',
            'builtin_id': 'language',
            'text': {},
            'text_key': 'MENU_LANGUAGE',
            'action': 'menu_language',
            'enabled': True,
            'visibility': 'all',
            'conditions': {'language_selection_enabled': True},
            'dynamic_text': False,
        },
        'admin_panel': {
            'type': 'builtin',
            'builtin_id': 'admin_panel',
            'text': {},
            'text_key': 'MENU_ADMIN',
            'action': 'admin_panel',
            'enabled': True,
            'visibility': 'admins',
            'conditions': None,
            'dynamic_text': False,
        },
        'moderator_panel': {
            'type': 'builtin',
            'builtin_id': 'moderator_panel',
            # Ключа локализации нет: легаси-меню хардкодит подпись строкой.
            'text': {'ru': '🧑‍⚖️ Модерация', 'en': '🧑‍⚖️ Moderation'},
            'text_key': None,
            'action': 'moderator_panel',
            'enabled': True,
            'visibility': 'moderators',
            'conditions': None,
            'dynamic_text': False,
        },
    },
}


# Информация о встроенных кнопках для API
BUILTIN_BUTTONS_INFO: list[dict[str, Any]] = [
    {
        'id': 'connect',
        'default_text': {'ru': '🔗 Подключиться', 'en': '🔗 Connect'},
        'callback_data': 'subscription_connect',
        'default_conditions': {'has_active_subscription': True, 'subscription_is_active': True},
        'supports_dynamic_text': False,
        'supports_direct_open': True,
    },
    {
        'id': 'happ_download',
        'default_text': {'ru': '⬇️ Скачать Happ', 'en': '⬇️ Download Happ'},
        'callback_data': 'subscription_happ_download',
        'default_conditions': {'happ_enabled': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'subscription',
        'default_text': {'ru': '📊 Подписка', 'en': '📊 Subscription'},
        'callback_data': 'menu_subscription',
        'default_conditions': {'has_active_subscription': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'buy_traffic',
        'default_text': {'ru': '📈 Докупить трафик', 'en': '📈 Buy traffic'},
        'callback_data': 'buy_traffic',
        'default_conditions': {'has_traffic_limit': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'balance',
        'default_text': {'ru': '💰 Баланс: {balance}', 'en': '💰 Balance: {balance}'},
        'callback_data': 'menu_balance',
        'default_conditions': None,
        'supports_dynamic_text': True,
    },
    {
        'id': 'trial',
        'default_text': {'ru': '🎁 Пробный период', 'en': '🎁 Free trial'},
        'callback_data': 'menu_trial',
        'default_conditions': {'show_trial': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'buy_subscription',
        'default_text': {'ru': '🛒 Купить подписку', 'en': '🛒 Buy subscription'},
        'callback_data': 'menu_buy',
        'default_conditions': {'show_buy': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'simple_subscription',
        'default_text': {'ru': '💳 Простая подписка', 'en': '💳 Simple subscription'},
        'callback_data': 'simple_subscription_purchase',
        'default_conditions': {'simple_subscription_enabled': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'resume_checkout',
        'default_text': {'ru': '↩️ Вернуться к оформлению', 'en': '↩️ Resume checkout'},
        'callback_data': 'return_to_saved_cart',
        'default_conditions': {'has_saved_cart': True},
        'supports_dynamic_text': False,
    },
    {
        'id': CUSTOM_BUTTONS_SLOT_ID,
        'default_text': {
            'ru': '⟨кнопки, переданные ботом⟩',
            'en': '⟨buttons passed by the bot⟩',
        },
        'callback_data': '',
        'default_conditions': None,
        'supports_dynamic_text': False,
    },
    {
        'id': 'promocode',
        'default_text': {'ru': '🎟️ Промокод', 'en': '🎟️ Promo code'},
        'callback_data': 'menu_promocode',
        'default_conditions': None,
        'supports_dynamic_text': False,
    },
    {
        'id': 'referrals',
        'default_text': {'ru': '👥 Рефералы', 'en': '👥 Referrals'},
        'callback_data': 'menu_referrals',
        'default_conditions': {'referral_enabled': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'contests',
        'default_text': {'ru': '🎲 Конкурсы', 'en': '🎲 Contests'},
        'callback_data': 'contests_menu',
        'default_conditions': {'contests_visible': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'support',
        'default_text': {'ru': '💬 Поддержка', 'en': '💬 Support'},
        'callback_data': 'menu_support',
        'default_conditions': {'support_enabled': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'activate',
        'default_text': {'ru': 'активировать', 'en': 'activate'},
        'callback_data': 'activate_button',
        'default_conditions': {'activate_button_visible': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'info',
        'default_text': {'ru': 'ℹ️ Инфо', 'en': 'ℹ️ Info'},
        'callback_data': 'menu_info',
        'default_conditions': None,
        'supports_dynamic_text': False,
    },
    {
        'id': 'language',
        'default_text': {'ru': '🌐 Язык', 'en': '🌐 Language'},
        'callback_data': 'menu_language',
        'default_conditions': {'language_selection_enabled': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'admin_panel',
        'default_text': {'ru': '⚙️ Админ панель', 'en': '⚙️ Admin panel'},
        'callback_data': 'admin_panel',
        'default_conditions': {'is_admin': True},
        'supports_dynamic_text': False,
    },
    {
        'id': 'moderator_panel',
        'default_text': {'ru': '🧑‍⚖️ Модерация', 'en': '🧑‍⚖️ Moderation'},
        'callback_data': 'moderator_panel',
        'default_conditions': {'is_moderator': True},
        'supports_dynamic_text': False,
    },
]


# Все доступные callback_data в боте (для добавления кастомных кнопок)
AVAILABLE_CALLBACKS: list[dict[str, Any]] = [
    # Меню
    {
        'callback_data': 'back_to_menu',
        'name': 'Назад в меню',
        'category': 'menu',
        'icon': '⬅️',
        'text': {'ru': '⬅️ Назад', 'en': '⬅️ Back'},
    },
    {
        'callback_data': 'menu_faq',
        'name': 'FAQ',
        'category': 'menu',
        'icon': '❓',
        'text': {'ru': '❓ FAQ', 'en': '❓ FAQ'},
    },
    {
        'callback_data': 'menu_info_promo_groups',
        'name': 'Промо-группы',
        'category': 'menu',
        'icon': '👥',
        'text': {'ru': '👥 Промо-группы', 'en': '👥 Promo groups'},
    },
    {
        'callback_data': 'menu_privacy_policy',
        'name': 'Политика конфиденциальности',
        'category': 'menu',
        'icon': '🔒',
        'text': {'ru': '🔒 Политика конфиденциальности', 'en': '🔒 Privacy Policy'},
    },
    {
        'callback_data': 'menu_public_offer',
        'name': 'Публичная оферта',
        'category': 'menu',
        'icon': '📜',
        'text': {'ru': '📜 Публичная оферта', 'en': '📜 Public Offer'},
    },
    {
        'callback_data': 'menu_rules',
        'name': 'Правила',
        'category': 'menu',
        'icon': '📋',
        'text': {'ru': '📋 Правила', 'en': '📋 Rules'},
    },
    {
        'callback_data': 'menu_server_status',
        'name': 'Статус серверов',
        'category': 'menu',
        'icon': '🖥️',
        'text': {'ru': '🖥️ Статус серверов', 'en': '🖥️ Server Status'},
    },
    # Баланс
    {
        'callback_data': 'balance_history',
        'name': 'История баланса',
        'category': 'balance',
        'icon': '📜',
        'text': {'ru': '📜 История', 'en': '📜 History'},
    },
    {
        'callback_data': 'balance_topup',
        'name': 'Пополнить баланс',
        'category': 'balance',
        'icon': '💳',
        'text': {'ru': '💳 Пополнить', 'en': '💳 Top up'},
    },
    # Подписка
    {
        'callback_data': 'subscription_extend',
        'name': 'Продлить подписку',
        'category': 'subscription',
        'icon': '📅',
        'text': {'ru': '📅 Продлить', 'en': '📅 Extend'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'subscription_autopay',
        'name': 'Автоплатёж',
        'category': 'subscription',
        'icon': '🔄',
        'text': {'ru': '🔄 Автоплатёж', 'en': '🔄 Autopay'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'subscription_settings',
        'name': 'Настройки подписки',
        'category': 'subscription',
        'icon': '⚙️',
        'text': {'ru': '⚙️ Настройки', 'en': '⚙️ Settings'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'open_subscription_link',
        'name': 'Показать ссылку подписки',
        'category': 'subscription',
        'icon': '🔗',
        'text': {'ru': '🔗 Показать ссылку', 'en': '🔗 Show link'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'subscription_add_countries',
        'name': 'Добавить страны',
        'category': 'subscription',
        'icon': '🌍',
        'text': {'ru': '🌍 Добавить страны', 'en': '🌍 Add countries'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'subscription_reset_traffic',
        'name': 'Сбросить трафик',
        'category': 'subscription',
        'icon': '🔄',
        'text': {'ru': '🔄 Сбросить трафик', 'en': '🔄 Reset traffic'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'subscription_switch_traffic',
        'name': 'Переключить трафик',
        'category': 'subscription',
        'icon': '🔀',
        'text': {'ru': '🔀 Переключить трафик', 'en': '🔀 Switch traffic'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'subscription_change_devices',
        'name': 'Изменить устройства',
        'category': 'subscription',
        'icon': '📱',
        'text': {'ru': '📱 Изменить устройства', 'en': '📱 Change devices'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'subscription_manage_devices',
        'name': 'Управление устройствами',
        'category': 'subscription',
        'icon': '📲',
        'text': {'ru': '📲 Управление устройствами', 'en': '📲 Manage devices'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'subscription_upgrade',
        'name': 'Улучшить подписку',
        'category': 'subscription',
        'icon': '⬆️',
        'text': {'ru': '⬆️ Улучшить', 'en': '⬆️ Upgrade'},
        'requires_subscription': True,
    },
    # Подключение устройств
    {
        'callback_data': 'device_guide_ios',
        'name': 'Инструкция iOS',
        'category': 'devices',
        'icon': '📱',
        'text': {'ru': '📱 iOS', 'en': '📱 iOS'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'device_guide_android',
        'name': 'Инструкция Android',
        'category': 'devices',
        'icon': '🤖',
        'text': {'ru': '🤖 Android', 'en': '🤖 Android'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'device_guide_windows',
        'name': 'Инструкция Windows',
        'category': 'devices',
        'icon': '💻',
        'text': {'ru': '💻 Windows', 'en': '💻 Windows'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'device_guide_mac',
        'name': 'Инструкция macOS',
        'category': 'devices',
        'icon': '🎯',
        'text': {'ru': '🎯 macOS', 'en': '🎯 macOS'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'device_guide_tv',
        'name': 'Инструкция Android TV',
        'category': 'devices',
        'icon': '📺',
        'text': {'ru': '📺 Android TV', 'en': '📺 Android TV'},
        'requires_subscription': True,
    },
    {
        'callback_data': 'device_guide_appletv',
        'name': 'Инструкция Apple TV',
        'category': 'devices',
        'icon': '📺',
        'text': {'ru': '📺 Apple TV', 'en': '📺 Apple TV'},
        'requires_subscription': True,
    },
    # Happ
    {
        'callback_data': 'happ_download_ios',
        'name': 'Скачать Happ iOS',
        'category': 'happ',
        'icon': '🍎',
        'text': {'ru': '🍎 iOS', 'en': '🍎 iOS'},
    },
    {
        'callback_data': 'happ_download_android',
        'name': 'Скачать Happ Android',
        'category': 'happ',
        'icon': '🤖',
        'text': {'ru': '🤖 Android', 'en': '🤖 Android'},
    },
    {
        'callback_data': 'happ_download_macos',
        'name': 'Скачать Happ macOS',
        'category': 'happ',
        'icon': '🖥️',
        'text': {'ru': '🖥️ macOS', 'en': '🖥️ macOS'},
    },
    {
        'callback_data': 'happ_download_windows',
        'name': 'Скачать Happ Windows',
        'category': 'happ',
        'icon': '💻',
        'text': {'ru': '💻 Windows', 'en': '💻 Windows'},
    },
    # Рефералы
    {
        'callback_data': 'referral_create_invite',
        'name': 'Создать инвайт',
        'category': 'referral',
        'icon': '✉️',
        'text': {'ru': '✉️ Создать инвайт', 'en': '✉️ Create invite'},
    },
    {
        'callback_data': 'referral_show_qr',
        'name': 'QR код реферала',
        'category': 'referral',
        'icon': '📱',
        'text': {'ru': '📱 QR код', 'en': '📱 QR code'},
    },
    {
        'callback_data': 'referral_list',
        'name': 'Список рефералов',
        'category': 'referral',
        'icon': '👥',
        'text': {'ru': '👥 Мои рефералы', 'en': '👥 My referrals'},
    },
    {
        'callback_data': 'referral_analytics',
        'name': 'Аналитика рефералов',
        'category': 'referral',
        'icon': '📊',
        'text': {'ru': '📊 Аналитика', 'en': '📊 Analytics'},
    },
    # Поддержка
    {
        'callback_data': 'create_ticket',
        'name': 'Создать тикет',
        'category': 'support',
        'icon': '✏️',
        'text': {'ru': '✏️ Создать тикет', 'en': '✏️ Create ticket'},
    },
    {
        'callback_data': 'my_tickets',
        'name': 'Мои тикеты',
        'category': 'support',
        'icon': '📋',
        'text': {'ru': '📋 Мои тикеты', 'en': '📋 My tickets'},
    },
    # Триал
    {
        'callback_data': 'trial_activate',
        'name': 'Активировать триал',
        'category': 'trial',
        'icon': '🎁',
        'text': {'ru': '🎁 Активировать', 'en': '🎁 Activate'},
    },
    # Покупка
    {
        'callback_data': 'clear_saved_cart',
        'name': 'Очистить корзину',
        'category': 'purchase',
        'icon': '🗑️',
        'text': {'ru': '🗑️ Очистить корзину', 'en': '🗑️ Clear cart'},
    },
    {
        'callback_data': 'subscription_confirm',
        'name': 'Подтвердить покупку',
        'category': 'purchase',
        'icon': '✅',
        'text': {'ru': '✅ Подтвердить', 'en': '✅ Confirm'},
    },
    {
        'callback_data': 'subscription_cancel',
        'name': 'Отменить покупку',
        'category': 'purchase',
        'icon': '❌',
        'text': {'ru': '❌ Отменить', 'en': '❌ Cancel'},
    },
]

# Динамические плейсхолдеры для текста кнопок
DYNAMIC_PLACEHOLDERS: list[dict[str, str]] = [
    {'placeholder': '{balance}', 'description': 'Баланс пользователя', 'example': '1 500 ₽', 'category': 'user'},
    {'placeholder': '{username}', 'description': 'Имя пользователя', 'example': 'John', 'category': 'user'},
    {
        'placeholder': '{subscription_days}',
        'description': 'Дней до окончания подписки',
        'example': '14',
        'category': 'subscription',
    },
    {
        'placeholder': '{traffic_used}',
        'description': 'Использованный трафик',
        'example': '5.2 GB',
        'category': 'subscription',
    },
    {
        'placeholder': '{traffic_left}',
        'description': 'Оставшийся трафик',
        'example': '94.8 GB',
        'category': 'subscription',
    },
    {'placeholder': '{referral_count}', 'description': 'Количество рефералов', 'example': '12', 'category': 'referral'},
    {
        'placeholder': '{referral_earnings}',
        'description': 'Заработок с рефералов',
        'example': '500 ₽',
        'category': 'referral',
    },
]
