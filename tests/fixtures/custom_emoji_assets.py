"""Сборка карты кастомных эмодзи из РЕАЛЬНЫХ ассетов по прод-конвейеру.

Прод на старте собирает карту заново: ключи из стикер-паков Telegram + `aliases.json`.
`assets/custom_emoji/map.json` — это СНИМОК, снятый уже ПОСЛЕ применения алиасов
(см. `_meta.aliases`), поэтому он содержит и alias-производные ключи. Отличить их
можно по id: у стикера пака id собственный, а алиас лишь копирует id своей цели.
Поэтому пак-частью считается всё, кроме математических символов (Sm), которые
дублируют чужой id: `→` (эхо `➡`) вычитается, а `↔` со своим id остаётся.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from typing import Final

from app.services.custom_emoji_pack_service import _apply_aliases
from app.utils.custom_emoji import EmojiMapping, build_mapping, load_mapping


#: Текст внутри одной entity кастомного эмодзи — именно его валидирует Telegram.
ENTITY_RE: Final[re.Pattern[str]] = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>')


def is_math_symbol_key(key: str) -> bool:
    return all(unicodedata.category(char) == 'Sm' for char in key)


def find_alias_echoes(snapshot: Mapping[str, str]) -> list[str]:
    """Ключи-эхо alias-слоя: математический символ, который лишь дублирует чужой id."""
    shared_ids = {emoji_id for emoji_id, seen in Counter(snapshot.values()).items() if seen > 1}
    return [key for key, emoji_id in snapshot.items() if is_math_symbol_key(key) and emoji_id in shared_ids]


def build_pack_only_map() -> dict[str, str]:
    snapshot = dict(load_mapping().emoji_map)
    echoes = set(find_alias_echoes(snapshot))
    return {key: emoji_id for key, emoji_id in snapshot.items() if key not in echoes}


def build_real_pack_mapping() -> EmojiMapping:
    """Собрать карту так, как её собирает прод: паки + `aliases.json`."""
    raw = build_pack_only_map()
    _apply_aliases(raw)
    return build_mapping(raw)
