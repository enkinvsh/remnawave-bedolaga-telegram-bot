"""Экран «Настройки бота → группа» не должен рождать невалидные entity кастомных эмодзи.

Прод падал с `TelegramBadRequest: Bad Request: ENTITY_TEXT_INVALID`, потому что
хлебная крошка `🏠 → {название}` оборачивала U+2192 в `<tg-emoji>`. Telegram валидирует
ТЕКСТ entity, а U+2192 — математический символ (Sm) без эмодзи-формы.
"""

import unicodedata

from app.handlers.admin.bot_configuration import (
    _get_group_description,
    _get_group_icon,
    _get_group_status,
    _get_grouped_categories,
)
from app.utils.custom_emoji import substitute_custom_emoji
from tests.fixtures.custom_emoji_assets import ENTITY_RE, build_real_pack_mapping


def _render_group_text(group_key: str, group_title: str) -> str:
    """Повторяет построение текста в `show_bot_config_group` (строки 1517-1549)."""
    status_icon, status_text = _get_group_status(group_key)
    description = _get_group_description(group_key)
    icon = _get_group_icon(group_key)
    raw_title = str(group_title).strip()
    clean_title = raw_title
    if icon and raw_title.startswith(icon):
        clean_title = raw_title[len(icon) :].strip()
    elif ' ' in raw_title:
        possible_icon, remainder = raw_title.split(' ', 1)
        if possible_icon:
            icon = possible_icon
            clean_title = remainder.strip()
    lines = [f'{icon} <b>{clean_title}</b>']
    if status_text:
        lines.append(f'Статус: {status_icon} {status_text}')
    lines.append(f'🏠 → {clean_title}')
    if description:
        lines.append('')
        lines.append(description)
    lines.append('')
    lines.append('📂 Категории группы:')
    return '\n'.join(lines)


def test_every_config_group_screen_has_only_valid_emoji_entities():
    mapping = build_real_pack_mapping()
    grouped = _get_grouped_categories()

    assert grouped, 'группы настроек не собрались — тест ничего не проверил бы'

    for group_key, group_title, _items in grouped:
        text = _render_group_text(group_key, group_title)
        substituted = substitute_custom_emoji(text, mapping=mapping)

        assert substituted is not None
        for entity_text in ENTITY_RE.findall(substituted):
            offenders = [char for char in entity_text if unicodedata.category(char) == 'Sm']
            assert not offenders, f'группа {group_key}: entity {entity_text!r} содержит {offenders!r}'


def test_breadcrumb_arrow_survives_as_plain_text():
    mapping = build_real_pack_mapping()
    group_key, group_title, _items = _get_grouped_categories()[0]

    substituted = substitute_custom_emoji(_render_group_text(group_key, group_title), mapping=mapping)

    assert substituted is not None
    assert '\u2192' in substituted
    assert '\u2192' not in ENTITY_RE.findall(substituted)
