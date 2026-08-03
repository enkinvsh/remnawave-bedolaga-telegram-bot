"""Подстановка кастомных эмодзи Telegram в исходящий HTML-текст.

Чистый модуль без зависимостей от aiogram: загрузка карты `эмодзи -> custom_emoji_id`,
сборка регулярного выражения и сама подстановка. Транспортный слой живёт в
`app/middlewares/custom_emoji_request.py`.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final

import structlog


logger = structlog.get_logger(__name__)

VS16: Final[str] = '\ufe0f'
ZWJ: Final[str] = '\u200d'
TAG_MARKER: Final[str] = '<tg-emoji'

#: Предохранитель под недокументированный лимит Telegram (~100 entity на сообщение).
#: Длину подстановка НЕ увеличивает: лимит текста считается ПОСЛЕ парсинга entity.
MAX_SUBSTITUTIONS_PER_FIELD: Final[int] = 50

_ASSETS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / 'assets' / 'custom_emoji'
DEFAULT_MAP_PATH: Final[Path] = _ASSETS_DIR / 'map.json'
DEFAULT_ALIASES_PATH: Final[Path] = _ASSETS_DIR / 'aliases.json'
DEFAULT_USAGE_PATH: Final[Path] = _ASSETS_DIR / 'usage.json'

_EMOJI_ID_RE: Final[re.Pattern[str]] = re.compile(r'^\d{1,32}$')

#: `<pre>` идёт ПЕРЕД `<code>`: внутри `<pre>` может лежать `<code>`.
#: Последняя ветка закрывает сами теги и значения их атрибутов.
_PROTECTED_RE: Final[re.Pattern[str]] = re.compile(
    r'<pre\b.*?</pre\s*>|<code\b.*?</code\s*>|<[^>]*>',
    re.IGNORECASE | re.DOTALL,
)

#: Символы, которые склеивают соседей в один графемный кластер.
_SKIN_TONES: Final[frozenset[str]] = frozenset({'\U0001f3fb', '\U0001f3fc', '\U0001f3fd', '\U0001f3fe', '\U0001f3ff'})
_LEADING_JOINERS: Final[frozenset[str]] = frozenset({ZWJ}) | _SKIN_TONES
_TRAILING_JOINERS: Final[frozenset[str]] = frozenset({ZWJ, VS16, '\ufe0e', '\u20e3'}) | _SKIN_TONES


@dataclass(frozen=True, slots=True)
class EmojiMapping:
    """Неизменяемая пара «карта эмодзи + скомпилированный шаблон»."""

    emoji_map: Mapping[str, str]
    pattern: re.Pattern[str] | None


_EMPTY_MAPPING: Final[EmojiMapping] = EmojiMapping(emoji_map=MappingProxyType({}), pattern=None)

_cached_mapping: EmojiMapping | None = None

#: Рантайм-переключатель из БД. None -> решает env-флаг (мастер kill switch на холодный старт).
_enabled_override: bool | None = None


def _read_json_section(path: Path, section: str) -> dict:
    """Прочитать секцию JSON-ассета. Любая ошибка -> пустой dict, исключение не поднимается."""
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        logger.warning('JSON-ассет кастомных эмодзи не найден', path=str(path), section=section)
        return {}
    except (OSError, ValueError) as error:
        logger.warning('Не удалось прочитать JSON-ассет', path=str(path), section=section, error=str(error))
        return {}

    data = payload.get(section) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        logger.warning('JSON-ассет имеет неверный формат', path=str(path), section=section)
        return {}
    return data


def load_aliases(path: Path | str | None = None) -> dict[str, str]:
    """Прочитать пак-НЕЗАВИСИМЫЕ замены «эмодзи -> эмодзи» (например 🔍 -> 🔎)."""
    target = Path(path) if path is not None else DEFAULT_ALIASES_PATH
    raw = _read_json_section(target, 'aliases')
    return {
        alias.replace(VS16, ''): value.replace(VS16, '')
        for alias, value in raw.items()
        if isinstance(alias, str) and isinstance(value, str) and alias.replace(VS16, '') and value.replace(VS16, '')
    }


def load_usage(path: Path | str | None = None) -> dict[str, int]:
    """Прочитать частотность эмодзи в текстах бота — по ней считается покрытие пака."""
    target = Path(path) if path is not None else DEFAULT_USAGE_PATH
    raw = _read_json_section(target, 'usage')
    return {
        emoji.replace(VS16, ''): count
        for emoji, count in raw.items()
        if isinstance(emoji, str) and emoji.replace(VS16, '') and isinstance(count, int) and not isinstance(count, bool)
    }


def set_enabled_override(value: bool | None) -> None:
    """Записать рантайм-состояние фичи (None = отдать решение env-флагу)."""
    global _enabled_override
    _enabled_override = value


def get_enabled_override() -> bool | None:
    """Прочитать рантайм-состояние фичи из памяти — без похода в БД (горячий путь)."""
    return _enabled_override


def build_mapping(raw: Mapping[str, object]) -> EmojiMapping:
    """Собрать неизменяемую карту и шаблон из сырых пар «эмодзи -> id»."""
    emoji_map: dict[str, str] = {}
    for emoji, emoji_id in raw.items():
        if not isinstance(emoji, str):
            logger.warning('Пропущен кастомный эмодзи с нестроковым ключом', emoji=repr(emoji))
            continue
        key = emoji.replace(VS16, '')
        if not key:
            logger.warning('Пропущен пустой ключ кастомного эмодзи', emoji=repr(emoji))
            continue
        if not isinstance(emoji_id, str) or not _EMOJI_ID_RE.match(emoji_id):
            logger.warning('Пропущен кастомный эмодзи с некорректным id', emoji=key, emoji_id=repr(emoji_id))
            continue
        emoji_map[key] = emoji_id

    if not emoji_map:
        return _EMPTY_MAPPING

    # Длинные альтернативы первыми: многокодпойнтовые последовательности должны
    # выигрывать у своего же базового префикса. VS16 допускается и внутри
    # последовательности (1️⃣ = '1' + VS16 + U+20E3), и в хвосте (⬅️).
    alternatives = (
        '(?:{})'.format(f'{VS16}?'.join(re.escape(codepoint) for codepoint in key))
        for key in sorted(emoji_map, key=len, reverse=True)
    )
    pattern = re.compile('(?:{}){}?'.format('|'.join(alternatives), VS16))
    return EmojiMapping(emoji_map=MappingProxyType(emoji_map), pattern=pattern)


def load_mapping(path: Path | str | None = None) -> EmojiMapping:
    """Прочитать карту с диска. Любая ошибка -> пустая карта, исключение не поднимается."""
    target = Path(path) if path is not None else DEFAULT_MAP_PATH
    try:
        payload = json.loads(target.read_text(encoding='utf-8'))
    except FileNotFoundError:
        logger.warning('Карта кастомных эмодзи не найдена', path=str(target))
        return _EMPTY_MAPPING
    except (OSError, ValueError) as error:
        logger.warning('Не удалось прочитать карту кастомных эмодзи', path=str(target), error=str(error))
        return _EMPTY_MAPPING

    raw = payload.get('map') if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        logger.warning('Карта кастомных эмодзи имеет неверный формат', path=str(target))
        return _EMPTY_MAPPING

    return build_mapping(raw)


def get_mapping() -> EmojiMapping:
    """Вернуть карту из модульного кеша, загрузив её при первом обращении."""
    global _cached_mapping
    if _cached_mapping is None:
        _cached_mapping = load_mapping()
    return _cached_mapping


def set_mapping(mapping: EmojiMapping) -> None:
    """Заменить активную карту целиком — атомарно, одним присваиванием.

    Вызывается админкой после пересборки карты из паков: подстановка начинает
    работать НЕМЕДЛЕННО, без рестарта. Карта собирается ДО присваивания, поэтому
    параллельные запросы всегда видят либо старую, либо новую карту целиком.
    """
    global _cached_mapping
    _cached_mapping = mapping


def reset_mapping_cache() -> None:
    """Сбросить модульный кеш карты — следующее обращение перечитает встроенный ассет."""
    global _cached_mapping
    _cached_mapping = None


def _is_cluster_boundary(text: str, start: int, end: int) -> bool:
    """Проверить, что совпадение не является частью более крупного графемного кластера."""
    if start > 0 and text[start - 1] in _LEADING_JOINERS:
        return False
    return not (end < len(text) and text[end] in _TRAILING_JOINERS)


def get_leading_emoji_id(text: str | None, mapping: EmojiMapping | None = None) -> str | None:
    """Вернуть custom_emoji_id для эмодзи В НАЧАЛЕ строки, если он есть в карте.

    Используется для иконок кнопок: Telegram рендерит `icon_custom_emoji_id` слева
    от лейбла, поэтому ведущий юникод-эмодзи из текста нужно убрать. Возвращает None,
    если строка не начинается с эмодзи, эмодзи нет в карте или совпадение является
    частью более крупного графемного кластера.
    """
    if not text:
        return None

    active = mapping if mapping is not None else get_mapping()
    if active.pattern is None:
        return None

    match = active.pattern.match(text)
    if match is None:
        return None
    if not _is_cluster_boundary(text, match.start(), match.end()):
        return None
    return active.emoji_map.get(match.group(0).replace(VS16, ''))


def _substitute_segment(segment: str, mapping: EmojiMapping, budget: list[int]) -> str:
    pattern = mapping.pattern
    if pattern is None or not segment:
        return segment

    def _replace(match: re.Match[str]) -> str:
        matched = match.group(0)
        if budget[0] <= 0:
            return matched
        if not _is_cluster_boundary(segment, match.start(), match.end()):
            return matched
        emoji_id = mapping.emoji_map.get(matched.replace(VS16, ''))
        if emoji_id is None:
            return matched
        budget[0] -= 1
        return f'<tg-emoji emoji-id="{emoji_id}">{matched}</tg-emoji>'

    return pattern.sub(_replace, segment)


def substitute_custom_emoji(text: str | None, mapping: EmojiMapping | None = None) -> str | None:
    """Заменить юникод-эмодзи на `<tg-emoji>`-разметку.

    Подстановка нейтральна по длине (Telegram считает лимит после парсинга entity),
    пропускает `<pre>`/`<code>`, сами теги и их атрибуты, не рвёт графемные кластеры
    и идемпотентна: уже размеченный текст возвращается без изменений.
    """
    if not text:
        return text

    active = mapping if mapping is not None else get_mapping()
    if active.pattern is None:
        return text
    if TAG_MARKER in text:
        return text

    budget = [MAX_SUBSTITUTIONS_PER_FIELD]
    chunks: list[str] = []
    cursor = 0
    for protected in _PROTECTED_RE.finditer(text):
        chunks.append(_substitute_segment(text[cursor : protected.start()], active, budget))
        chunks.append(protected.group(0))
        cursor = protected.end()
    chunks.append(_substitute_segment(text[cursor:], active, budget))
    return ''.join(chunks)
