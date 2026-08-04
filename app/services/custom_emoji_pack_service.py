"""Рантайм-настройка паков кастомных эмодзи: ссылка из админки -> активная карта.

Карта эмодзи собирается из стикер-паков Telegram по требованию администратора и
подменяет встроенный ассет `assets/custom_emoji/map.json` без редеплоя. Слой
алиасов (`assets/custom_emoji/aliases.json`) точно так же перекрывается правками
оператора: в настройке лежит ПОЛНАЯ карта алиасов, поэтому вредный алиас можно
именно УДАЛИТЬ, а не только добавить новый.
Состояние хранится в `system_settings` (миграция не нужна — ключи произвольные).
"""

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.system_setting import delete_system_setting, get_setting_value, upsert_system_setting
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
CUSTOM_EMOJI_ALIASES_KEY: Final[str] = 'custom_emoji_aliases'

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


_ALIAS_SEPARATORS: Final[tuple[str, ...]] = ('->', '=>', '\u2192', '=')

#: Правки алиасов оператором. None -> действует встроенный `aliases.json`.
_aliases_override: dict[str, str] | None = None


class PackError(Exception):
    """Базовая ошибка работы с паком кастомных эмодзи."""


class AliasError(Exception):
    """Алиас отвергнут. Сообщение готово к показу оператору как есть."""


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


def _is_math_symbol_key(key: str) -> bool:
    return all(unicodedata.category(char) == 'Sm' for char in key)


def _has_non_emoji_character(key: str) -> bool:
    """Буква, пробел или управляющий символ в ключе — тот же отказ Telegram, что и у `→`, только хуже.

    Ключ `a` обернул бы в `<tg-emoji>` КАЖДУЮ букву `a` в тексте и положил бы всё подряд.
    Цифры не запрещены: они законная часть keycap-последовательности (`1` + U+20E3 = 1⃣).
    """
    return any(unicodedata.category(char)[0] in ('L', 'Z', 'C') for char in key)


def set_aliases_override(value: Mapping[str, str] | None) -> None:
    """Записать активную карту алиасов (None = отдать решение встроенному файлу)."""
    global _aliases_override
    _aliases_override = dict(value) if value is not None else None


def get_aliases_override() -> dict[str, str] | None:
    """Прочитать правки оператора из памяти — без похода в БД (горячий путь сборки карты)."""
    return dict(_aliases_override) if _aliases_override is not None else None


def get_effective_aliases() -> dict[str, str]:
    """Активная карта алиасов: правки оператора, иначе встроенный ассет образа."""
    if _aliases_override is not None:
        return dict(_aliases_override)
    return load_aliases()


def _apply_aliases(mapping: dict[str, str]) -> None:
    """Слой пак-независимых замен: алиас получает id цели, если та есть, а алиаса ещё нет."""
    for alias, target in get_effective_aliases().items():
        if alias in mapping:
            continue
        # Telegram валидирует ТЕКСТ entity, а не id: если сам ключ-алиас не эмодзи
        # (например → U+2192 — математическая стрелка без эмодзи-формы, эмодзи-стрелка
        # это ➡ U+27A1), то <tg-emoji> с таким текстом рушит ЛЮБОЕ сообщение с этим
        # символом — ENTITY_TEXT_INVALID. Ключи из паков сюда не попадают: их Telegram
        # назначил стикерам сам, поэтому они валидны по построению.
        if _is_math_symbol_key(alias):
            logger.warning('Алиас кастомного эмодзи отклонён: ключ не эмодзи', alias=alias)
            continue
        target_id = mapping.get(target)
        if target_id is not None:
            mapping[alias] = target_id


def parse_alias_pair(raw: Any) -> tuple[str, str] | None:
    """Разобрать ввод оператора «эмодзи -> эмодзи» в пару ключ/цель."""
    if not isinstance(raw, str):
        return None

    text = raw
    for separator in _ALIAS_SEPARATORS:
        text = text.replace(separator, ' ')

    parts = text.split()
    if len(parts) != 2:
        return None

    alias, target = (part.replace(_VS16, '') for part in parts)
    return (alias, target) if alias and target else None


def validate_aliases(aliases: Mapping[str, str]) -> dict[str, str]:
    """Проверить ПОЛНУЮ карту алиасов перед записью. Бросает `AliasError` с готовым текстом.

    Гард на ключ-математический-символ живёт здесь, а не только в `_apply_aliases`:
    иначе оператор мог бы вернуть через админку тот самый `→`, который клал весь бот.
    """
    clean: dict[str, str] = {}
    for raw_alias, raw_target in aliases.items():
        alias = raw_alias.replace(_VS16, '') if isinstance(raw_alias, str) else ''
        target = raw_target.replace(_VS16, '') if isinstance(raw_target, str) else ''

        if not alias or not target:
            raise AliasError('Пустой ключ или пустая цель алиаса.')
        if _is_math_symbol_key(alias):
            raise AliasError(
                f'Ключ «{alias}» — математический символ, а не эмодзи. Telegram проверяет ТЕКСТ '
                f'кастомного эмодзи и отвергает всё сообщение целиком (ENTITY_TEXT_INVALID). '
                f'Возьмите эмодзи-вариант символа.'
            )
        if _has_non_emoji_character(alias):
            raise AliasError(
                f'Ключ «{alias}» содержит букву, пробел или служебный символ — это не эмодзи. '
                f'Telegram проверяет ТЕКСТ кастомного эмодзи и отвергнет всё сообщение целиком '
                f'(ENTITY_TEXT_INVALID) везде, где встретится этот символ.'
            )
        if alias == target:
            raise AliasError(f'Алиас «{alias}» указывает сам на себя.')
        clean[alias] = target

    return clean


async def get_aliases(db: AsyncSession) -> dict[str, str]:
    """Прочитать активную карту алиасов. Настройки нет -> встроенный файл, БД при этом НЕ засевается.

    Пустая сохранённая карта (`{}`) — законное состояние «оператор снёс все алиасы»,
    а не «настройки нет»: именно поэтому проверяется `raw is None`, а не пустота карты.
    """
    raw = await get_setting_value(db, CUSTOM_EMOJI_ALIASES_KEY)
    if raw is None:
        return load_aliases()

    try:
        payload = json.loads(raw)
    except ValueError as error:
        logger.warning('Сохранённая карта алиасов не читается, берём встроенную', error=str(error))
        return load_aliases()

    if not isinstance(payload, dict):
        logger.warning('Сохранённая карта алиасов имеет неверный формат, берём встроенную')
        return load_aliases()

    return {
        alias.replace(_VS16, ''): target.replace(_VS16, '')
        for alias, target in payload.items()
        if isinstance(alias, str) and isinstance(target, str) and alias.replace(_VS16, '') and target.replace(_VS16, '')
    }


async def is_aliases_customized(db: AsyncSession) -> bool:
    """Правил ли алиасы оператор (иначе действует файл из образа)."""
    return await get_setting_value(db, CUSTOM_EMOJI_ALIASES_KEY) is not None


async def save_aliases(db: AsyncSession, aliases: Mapping[str, str]) -> dict[str, str]:
    """Записать ПОЛНУЮ карту алиасов и сразу обновить рантайм-кеш."""
    clean = validate_aliases(aliases)
    await upsert_system_setting(
        db,
        CUSTOM_EMOJI_ALIASES_KEY,
        json.dumps(clean, ensure_ascii=False, sort_keys=True),
        description='Алиасы кастомных эмодзи, полная карта (настройки нет = встроенный aliases.json)',
    )
    set_aliases_override(clean)
    return clean


async def add_alias(db: AsyncSession, alias: str, target: str) -> dict[str, str]:
    """Добавить алиас поверх текущей карты.

    Неизвестная цель НЕ блокируется: `_apply_aliases` подставляет id только когда цель
    есть в карте, поэтому такой алиас инертен и станет рабочим, когда приедет нужный пак.
    Вредным бывает КЛЮЧ (его текст уходит в entity) — его и отвергаем жёстко.
    """
    key = alias.replace(_VS16, '').strip()
    value = target.replace(_VS16, '').strip()

    current = await get_aliases(db)
    if key in current:
        raise AliasError(f'Алиас «{key}» уже задан: сначала удалите его.')

    return await save_aliases(db, {**current, key: value})


async def remove_alias(db: AsyncSession, alias: str) -> dict[str, str]:
    """Убрать алиас из карты. Именно это спасает от вредного алиаса без редеплоя."""
    key = alias.replace(_VS16, '').strip()

    current = await get_aliases(db)
    if key not in current:
        raise AliasError(f'Алиаса «{key}» нет в текущей карте.')

    return await save_aliases(db, {name: value for name, value in current.items() if name != key})


async def reset_aliases(db: AsyncSession) -> dict[str, str]:
    """Стереть правки оператора: возвращается ровно содержимое `assets/custom_emoji/aliases.json`."""
    await delete_system_setting(db, CUSTOM_EMOJI_ALIASES_KEY)
    set_aliases_override(None)
    return load_aliases()


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

        set_aliases_override(await get_aliases(db))

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
