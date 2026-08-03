"""Рантайм-настройка паков кастомных эмодзи: ссылка из админки -> активная карта.

Карта эмодзи собирается из стикер-паков Telegram по требованию администратора и
подменяет встроенный ассет `assets/custom_emoji/map.json` без редеплоя.
Состояние хранится в `system_settings` (миграция не нужна — ключи произвольные).
"""

import re
from typing import Any, Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.system_setting import get_setting_value, upsert_system_setting
from app.utils.custom_emoji import (
    build_mapping,
    get_mapping,
    load_aliases,
    load_mapping,
    load_usage,
    set_enabled_override,
    set_mapping,
)


logger = structlog.get_logger(__name__)

CUSTOM_EMOJI_PACKS_KEY: Final[str] = 'custom_emoji_packs'
CUSTOM_EMOJI_ENABLED_KEY: Final[str] = 'custom_emoji_enabled'

PACK_NAME_RE: Final[re.Pattern[str]] = re.compile(r'^[A-Za-z0-9_]{1,64}$')
_EMOJI_ID_RE: Final[re.Pattern[str]] = re.compile(r'^\d{1,32}$')
_VS16: Final[str] = '\ufe0f'
_CUSTOM_EMOJI_STICKER_TYPE: Final[str] = 'custom_emoji'

_LINK_PREFIXES: Final[tuple[str, ...]] = (
    'https://t.me/addemoji/',
    'http://t.me/addemoji/',
    'https://telegram.me/addemoji/',
    'http://telegram.me/addemoji/',
    't.me/addemoji/',
    'telegram.me/addemoji/',
    'tg://addemoji?set=',
)


class PackError(Exception):
    """Базовая ошибка работы с паком кастомных эмодзи."""


class PackLinkError(PackError):
    """Ссылка на пак не распознана."""


class PackNotFoundError(PackError):
    """Telegram не отдал такой стикерсет."""


class PackTypeError(PackError):
    """Стикерсет существует, но это не пак кастомных эмодзи."""


def parse_pack_link(raw: Any) -> str | None:
    """Достать имя пака из ссылки любой формы или из голого имени."""
    if not isinstance(raw, str):
        return None

    candidate = raw.strip()
    if not candidate:
        return None

    lowered = candidate.lower()
    for prefix in _LINK_PREFIXES:
        if lowered.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    else:
        if '/' in candidate or '?' in candidate or ':' in candidate:
            return None

    candidate = candidate.split('?', 1)[0].split('#', 1)[0].strip('/').strip()
    return candidate if PACK_NAME_RE.match(candidate) else None


async def fetch_pack(bot: Any, name: str) -> list[tuple[str, str]]:
    """Забрать пары «эмодзи -> custom_emoji_id» из пака Telegram."""
    pack_name = parse_pack_link(name)
    if pack_name is None:
        raise PackLinkError(name)

    try:
        sticker_set = await bot.get_sticker_set(pack_name)
    except Exception as error:
        raise PackNotFoundError(pack_name) from error

    if getattr(sticker_set, 'sticker_type', None) != _CUSTOM_EMOJI_STICKER_TYPE:
        raise PackTypeError(pack_name)

    pairs: list[tuple[str, str]] = []
    for item in getattr(sticker_set, 'stickers', None) or []:
        emoji = getattr(item, 'emoji', None)
        emoji_id = getattr(item, 'custom_emoji_id', None)
        if isinstance(emoji, str) and isinstance(emoji_id, str):
            pairs.append((emoji, emoji_id))
    return pairs


async def build_mapping_from_packs(bot: Any, names: list[str]) -> dict[str, str]:
    """Собрать карту из паков в порядке приоритета: ПЕРВЫЙ пак выигрывает конфликт."""
    mapping: dict[str, str] = {}

    for name in names:
        try:
            pairs = await fetch_pack(bot, name)
        except PackError as error:
            logger.warning('Пак кастомных эмодзи недоступен', pack=name, error=type(error).__name__)
            continue

        for emoji, emoji_id in pairs:
            key = emoji.replace(_VS16, '')
            if not key or not _EMOJI_ID_RE.match(emoji_id):
                continue
            mapping.setdefault(key, emoji_id)

    _apply_aliases(mapping)
    return mapping


def _apply_aliases(mapping: dict[str, str]) -> None:
    """Слой пак-независимых замен: алиас получает id цели, если та есть, а алиаса ещё нет."""
    for alias, target in load_aliases().items():
        if alias in mapping:
            continue
        target_id = mapping.get(target)
        if target_id is not None:
            mapping[alias] = target_id


def compute_coverage(mapping: dict[str, str]) -> dict[str, int]:
    """Посчитать покрытие карты по фактической частотности эмодзи в текстах бота."""
    usage = load_usage()
    occ_total = sum(usage.values())
    occ_hit = sum(count for emoji, count in usage.items() if emoji in mapping)
    unique_hit = sum(1 for emoji in usage if emoji in mapping)

    return {
        'unique_hit': unique_hit,
        'unique_total': len(usage),
        'occ_hit': occ_hit,
        'occ_total': occ_total,
        'percent': round(100 * occ_hit / occ_total) if occ_total else 0,
    }


def _summary(source: str, packs: list[str], mapping: dict[str, str]) -> dict[str, Any]:
    return {
        'source': source,
        'packs': packs,
        'count': len(mapping),
        'coverage': compute_coverage(mapping),
    }


async def get_pack_names(db: AsyncSession) -> list[str]:
    """Прочитать список паков в порядке приоритета. Пусто -> используется встроенный ассет."""
    raw = await get_setting_value(db, CUSTOM_EMOJI_PACKS_KEY)
    if not raw:
        return []
    names: list[str] = []
    for chunk in raw.split(','):
        name = parse_pack_link(chunk)
        if name and name not in names:
            names.append(name)
    return names


async def save_pack_names(db: AsyncSession, names: list[str]) -> None:
    await upsert_system_setting(
        db,
        CUSTOM_EMOJI_PACKS_KEY,
        ','.join(names),
        description='Паки кастомных эмодзи в порядке приоритета (пусто = встроенная карта)',
    )


async def is_enabled(db: AsyncSession) -> bool:
    """Состояние фичи: значение из БД, иначе env-флаг."""
    raw = await get_setting_value(db, CUSTOM_EMOJI_ENABLED_KEY)
    if raw is None:
        from app.config import settings

        return bool(settings.CUSTOM_EMOJI_ENABLED)
    return raw.strip() == '1'


async def set_enabled(db: AsyncSession, value: bool) -> None:
    """Сохранить состояние фичи и сразу обновить кеш, который читает middleware."""
    await upsert_system_setting(
        db,
        CUSTOM_EMOJI_ENABLED_KEY,
        '1' if value else '0',
        description='Подстановка кастомных эмодзи в исходящих сообщениях',
    )
    set_enabled_override(value)


def apply_bundled_mapping() -> dict[str, Any]:
    """Вернуться на встроенный ассет."""
    mapping = load_mapping()
    set_mapping(mapping)
    return _summary('bundled', [], dict(mapping.emoji_map))


async def load_and_apply(bot: Any, db: AsyncSession) -> dict[str, Any]:
    """Прочитать настройки из БД и установить активную карту. НИКОГДА не бросает исключение."""
    try:
        raw_enabled = await get_setting_value(db, CUSTOM_EMOJI_ENABLED_KEY)
        if raw_enabled is not None:
            set_enabled_override(raw_enabled.strip() == '1')

        names = await get_pack_names(db)
        if not names:
            return apply_bundled_mapping()

        raw_mapping = await build_mapping_from_packs(bot, names)
        if not raw_mapping:
            logger.warning('Паки не дали ни одной иконки, карта не меняется', packs=names)
            return _summary('unchanged', names, dict(get_mapping().emoji_map))

        set_mapping(build_mapping(raw_mapping))
        return _summary('packs', names, raw_mapping)

    except Exception as error:
        logger.warning(
            'Не удалось применить настройки кастомных эмодзи, активная карта сохранена',
            error=str(error),
            error_type=type(error).__name__,
        )
        return _summary('unchanged', [], dict(get_mapping().emoji_map))


async def rebuild_and_apply(bot: Any, names: list[str]) -> dict[str, Any]:
    """Пересобрать карту под заданный список паков и включить её немедленно."""
    if not names:
        return apply_bundled_mapping()

    raw_mapping = await build_mapping_from_packs(bot, names)
    if not raw_mapping:
        raise PackNotFoundError(','.join(names))

    set_mapping(build_mapping(raw_mapping))
    return _summary('packs', names, raw_mapping)
