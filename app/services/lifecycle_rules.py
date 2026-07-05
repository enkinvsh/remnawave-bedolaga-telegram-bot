"""Реестр правил lifecycle-рассылок — единственный источник дефолтов.

Здесь и ТОЛЬКО здесь живут дефолтные тайминги, проценты и тексты правил
жизненного цикла клиента. Строка в таблице ``lifecycle_rules`` — это ОВЕРРАЙД
поверх дефолта отсюда, а не полный набор настроек. Пустая таблица = чистые
дефолты, поэтому существующие потребители ведут себя идентично без единой строки
в БД (обратная совместимость).

Это SaaS-продукт под разных клиентов: ничего не хардкодится в вызывающем коде —
всё редактируется через кабинет и читается из этого реестра + БД-оверрайдов.

Плейсхолдеры в ``message_template``'ах (подставляются триггером — задача C2):
    {days}    — количество дней (до/после события)
    {percent} — процент скидки
    {date}    — дата (окончания триала / действия скидки)
    {hours}   — количество часов
"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


# Валидные группы правил в порядке жизненного цикла клиента.
GROUPS: tuple[str, ...] = ('pre_trial', 'in_trial', 'post_trial', 'paid')

# Плейсхолдеры, поддерживаемые в текстах правил. Документируются для кабинета,
# чтобы оператор видел доступные подстановки при редактировании шаблона.
PLACEHOLDERS: dict[str, str] = {
    '{days}': 'количество дней (до/после события)',
    '{percent}': 'процент скидки',
    '{date}': 'дата (окончания триала / действия скидки)',
    '{hours}': 'количество часов',
}


@dataclass(frozen=True, slots=True)
class RuleDescriptor:
    """Декларативное описание одного lifecycle-правила (дефолт).

    Attributes:
        key: Уникальный ключ правила (совпадает с ключом в реестре и БД).
        group: Группа жизненного цикла (одна из ``GROUPS``).
        enabled: Включено ли правило по умолчанию.
        config: Дефолтная конфигурация (тайминги/проценты/тексты). БЕЗ поля
            ``enabled`` — оно хранится отдельной колонкой.
        placeholders: Плейсхолдеры, которые использует шаблон(ы) правила
            (для подсказки в кабинете; подмножество ключей ``PLACEHOLDERS``).
    """

    key: str
    group: str
    enabled: bool
    config: dict[str, Any]
    placeholders: tuple[str, ...] = field(default_factory=tuple)


DEFAULTS: dict[str, RuleDescriptor] = {
    # ── До триала ─────────────────────────────────────────────────────────
    'trial_not_activated': RuleDescriptor(
        key='trial_not_activated',
        group='pre_trial',
        enabled=True,
        config={
            'first_offset_hours': 1,
            'repeat_hours': 48,
            'max_repeats': 3,
            'message_template': ('У тебя есть бесплатный пробный доступ. Активируй сейчас — это 1 клик, без оплаты.'),
            'repeat_message_template': (
                '🎁 Ты ещё не активировал бесплатный пробный VPN! Попробуй — 1 клик, без оплаты и привязки карты.'
            ),
            'button': 'activate_trial',
        },
    ),
    # ── В триале ──────────────────────────────────────────────────────────
    'trial_zero_traffic': RuleDescriptor(
        key='trial_zero_traffic',
        group='in_trial',
        enabled=True,
        config={
            'offsets_hours': [1, 24],
            'message_templates': {
                '1': ('Пробный VPN активирован, но трафика пока ноль. Давай подключимся — это пара минут.'),
                '24': (
                    'Пробный доступ уже идёт, а ты ещё не подключился. Настрой VPN, чтобы не терять бесплатные дни.'
                ),
            },
            'buttons': ['connect', 'my_subscription', 'support'],
        },
    ),
    'trial_ending': RuleDescriptor(
        key='trial_ending',
        group='in_trial',
        enabled=True,
        config={
            'hours_before': 2,
            'message_template': (
                'Через {hours} ч. заканчивается пробный доступ. Оформи подписку, чтобы не остаться без VPN.'
            ),
        },
        placeholders=('{hours}',),
    ),
    # ── После триала ──────────────────────────────────────────────────────
    'post_trial_ladder': RuleDescriptor(
        key='post_trial_ladder',
        group='post_trial',
        enabled=True,
        config={
            'steps': [
                {'offset_hours': 1, 'discount_percent': 0},
                {'offset_hours': 24, 'discount_percent': 5, 'valid_hours': 24},
                {'offset_days': 3, 'discount_percent': 10, 'valid_hours': 24},
                {'offset_days': 7, 'discount_percent': 15, 'valid_hours': 24},
                {'offset_days': 14, 'discount_percent': 20, 'valid_hours': 24},
            ],
            'message_template': (
                'Пробный период закончился. Держи скидку {percent}% — успей оформить подписку за {hours} ч.'
            ),
            'message_template_no_discount': (
                'Пробный период закончился. Понравился VPN? Оформи подписку, чтобы вернуть доступ.'
            ),
        },
        placeholders=('{percent}', '{hours}'),
    ),
    # ── Платная подписка (миграция существующих ключей NotificationSettingsService) ──
    # Форма config сохранена 1:1 с легаси-дефолтами, чтобы monitoring_service и
    # бот-панель работали идентично. Тексты этих правил берутся из существующих
    # шаблонов бота (fallback — задача C2), поэтому здесь их нет.
    'expired_1d': RuleDescriptor(
        key='expired_1d',
        group='paid',
        enabled=True,
        config={},
    ),
    'expired_second_wave': RuleDescriptor(
        key='expired_second_wave',
        group='paid',
        enabled=True,
        config={'discount_percent': 10, 'valid_hours': 24},
        placeholders=('{percent}', '{hours}'),
    ),
    'expired_third_wave': RuleDescriptor(
        key='expired_third_wave',
        group='paid',
        enabled=True,
        config={'discount_percent': 20, 'valid_hours': 24, 'trigger_days': 5},
        placeholders=('{percent}', '{hours}', '{days}'),
    ),
    'trial_channel_unsubscribed': RuleDescriptor(
        key='trial_channel_unsubscribed',
        group='paid',
        enabled=True,
        config={},
    ),
}


def all_keys() -> tuple[str, ...]:
    """Все ключи правил в порядке объявления."""
    return tuple(DEFAULTS.keys())


def keys_for_group(group: str) -> tuple[str, ...]:
    """Ключи правил конкретной группы."""
    return tuple(key for key, descriptor in DEFAULTS.items() if descriptor.group == group)


def is_known_key(key: str) -> bool:
    """Зарегистрирован ли ключ в реестре."""
    return key in DEFAULTS


def get_descriptor(key: str) -> RuleDescriptor | None:
    """Дескриптор правила или ``None``, если ключ неизвестен."""
    return DEFAULTS.get(key)


def default_enabled(key: str) -> bool:
    """Дефолтный флаг ``enabled`` правила (``True`` для неизвестных — безопасно)."""
    descriptor = DEFAULTS.get(key)
    return descriptor.enabled if descriptor is not None else True


def default_config(key: str) -> dict[str, Any]:
    """Глубокая копия дефолтного config'а (мутировать безопасно)."""
    descriptor = DEFAULTS.get(key)
    return deepcopy(descriptor.config) if descriptor is not None else {}


def merged_config(key: str, override: dict[str, Any] | None) -> dict[str, Any]:
    """Дефолтный config, поверх которого наложен БД-оверрайд (shallow-merge).

    Возвращает всегда новый словарь. ``override`` (частичный набор полей из БД)
    затирает соответствующие ключи дефолта; вложенные структуры заменяются
    целиком (кабинет присылает config полностью на PUT).
    """
    config = default_config(key)
    if override:
        config.update(override)
    return config
