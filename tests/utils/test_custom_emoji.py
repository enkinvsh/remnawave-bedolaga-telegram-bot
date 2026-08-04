"""Тесты чистого слоя подстановки кастомных эмодзи (без aiogram)."""

import json
from pathlib import Path

import pytest

from app.services.custom_emoji_pack_service import apply_bundled_mapping
from app.utils.custom_emoji import (
    MAX_SUBSTITUTIONS_PER_FIELD,
    build_mapping,
    get_enabled_override,
    get_leading_emoji_id,
    get_mapping,
    load_aliases,
    load_mapping,
    load_usage,
    reset_mapping_cache,
    set_enabled_override,
    set_mapping,
    substitute_custom_emoji,
)
from tests.fixtures.custom_emoji_assets import (
    ENTITY_RE,
    build_real_pack_mapping,
    find_alias_echoes,
    is_math_symbol_key,
)


CHECK_ID = '5897607722295103204'
CROSS_ID = '5895462764903090781'
LEFT_ID = '5897428285412421110'


@pytest.fixture
def mapping():
    return build_mapping({'✅': CHECK_ID, '❌': CROSS_ID, '⬅': LEFT_ID})


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_mapping_cache()
    set_enabled_override(None)
    yield
    reset_mapping_cache()
    set_enabled_override(None)


def test_module_does_not_import_aiogram():
    from app.utils import custom_emoji as module

    content = Path(str(module.__file__)).read_text(encoding='utf-8')
    import_lines = [line for line in content.splitlines() if line.startswith(('import ', 'from '))]

    assert not [line for line in import_lines if 'aiogram' in line]


def test_simple_substitution(mapping):
    result = substitute_custom_emoji('✅ готово', mapping=mapping)

    assert result == f'<tg-emoji emoji-id="{CHECK_ID}">✅</tg-emoji> готово'


def test_vs16_stays_inside_the_tag(mapping):
    """⬅️ (U+2B05 U+FE0F): VS16 обязан попасть ВНУТРЬ тега, не осиротеть снаружи."""
    result = substitute_custom_emoji('⬅️ назад', mapping=mapping)

    assert result == f'<tg-emoji emoji-id="{LEFT_ID}">⬅\ufe0f</tg-emoji> назад'
    assert '</tg-emoji>\ufe0f' not in result
    assert result.count('\ufe0f') == 1


def test_unmapped_emoji_untouched(mapping):
    result = substitute_custom_emoji('🦄 всё ещё юникод', mapping=mapping)

    assert result == '🦄 всё ещё юникод'


def test_code_and_pre_blocks_protected(mapping):
    text = '<code>✅ code</code> ✅ <pre>✅ pre</pre>'
    result = substitute_custom_emoji(text, mapping=mapping)

    assert result == f'<code>✅ code</code> <tg-emoji emoji-id="{CHECK_ID}">✅</tg-emoji> <pre>✅ pre</pre>'


def test_nested_pre_code_protected(mapping):
    text = '<pre><code class="language-python">✅</code></pre> ✅'
    result = substitute_custom_emoji(text, mapping=mapping)

    assert result.startswith('<pre><code class="language-python">✅</code></pre>')
    assert result.endswith(f'<tg-emoji emoji-id="{CHECK_ID}">✅</tg-emoji>')


def test_attribute_values_untouched(mapping):
    text = '<a href="https://example.com/?q=✅">ссылка ✅</a>'
    result = substitute_custom_emoji(text, mapping=mapping)

    assert '<a href="https://example.com/?q=✅">' in result
    assert f'ссылка <tg-emoji emoji-id="{CHECK_ID}">✅</tg-emoji>' in result


def test_idempotent(mapping):
    once = substitute_custom_emoji('✅ ❌ ✅', mapping=mapping)
    twice = substitute_custom_emoji(once, mapping=mapping)

    assert twice == once


def test_already_substituted_text_unchanged(mapping):
    text = f'<tg-emoji emoji-id="{CHECK_ID}">✅</tg-emoji> и ещё ✅'
    result = substitute_custom_emoji(text, mapping=mapping)

    assert result == text


def test_regional_indicator_flag_unchanged(mapping):
    result = substitute_custom_emoji('🇺🇸 флаг', mapping=mapping)

    assert result == '🇺🇸 флаг'


def test_zwj_sequence_not_split(mapping):
    """👨 замаплен, 👨‍🎨 — нет: последовательность ZWJ обязана остаться целой."""
    zwj_mapping = build_mapping({'👨': '5000000000000000001', '🎨': '5000000000000000002'})

    result = substitute_custom_emoji('👨\u200d🎨', mapping=zwj_mapping)

    assert result == '👨\u200d🎨'


def test_keycap_not_split():
    """Цифра замаплена, кейкап 1⃣ — нет: комбинирующий U+20E3 нельзя отрывать."""
    digit_mapping = build_mapping({'1': '5000000000000000003'})

    assert substitute_custom_emoji('1\u20e3', mapping=digit_mapping) == '1\u20e3'
    assert substitute_custom_emoji('1\ufe0f\u20e3', mapping=digit_mapping) == '1\ufe0f\u20e3'


def test_skin_tone_modifier_not_split():
    skin_mapping = build_mapping({'👍': '5000000000000000004'})

    assert substitute_custom_emoji('👍\U0001f3fd', mapping=skin_mapping) == '👍\U0001f3fd'


def test_whole_cluster_from_map_is_wrapped_as_one_tag():
    """Если ZWJ-последовательность есть в карте целиком — она оборачивается ОДНИМ тегом."""
    zwj_mapping = build_mapping({'👨': '5000000000000000001', '👨\u200d🎨': '5000000000000000005'})

    result = substitute_custom_emoji('👨\u200d🎨', mapping=zwj_mapping)

    assert result == '<tg-emoji emoji-id="5000000000000000005">👨\u200d🎨</tg-emoji>'
    assert result.count('<tg-emoji') == 1


def test_interior_vs16_is_matched_and_kept_inside_tag():
    """Каноническая форма 1️⃣ = '1' + VS16 + U+20E3: VS16 внутри последовательности."""
    keycap_mapping = build_mapping({'1\u20e3': '5000000000000000006'})

    assert substitute_custom_emoji('1\u20e3', mapping=keycap_mapping) == (
        '<tg-emoji emoji-id="5000000000000000006">1\u20e3</tg-emoji>'
    )
    assert substitute_custom_emoji('1\ufe0f\u20e3', mapping=keycap_mapping) == (
        '<tg-emoji emoji-id="5000000000000000006">1\ufe0f\u20e3</tg-emoji>'
    )


def test_real_asset_keeps_grapheme_clusters_intact():
    """С реальной картой кластеры либо оборачиваются целиком, либо остаются нетронутыми."""
    assert substitute_custom_emoji('🇺🇸') == '🇺🇸'
    assert substitute_custom_emoji('👨\u200d🚀') == '👨\u200d🚀'
    assert substitute_custom_emoji('👍\U0001f3fd') == '👍\U0001f3fd'

    artist = substitute_custom_emoji('👨\u200d🎨')
    assert artist is not None
    assert artist.count('<tg-emoji') == 1
    assert '👨\u200d🎨</tg-emoji>' in artist


def test_longest_alternative_wins():
    zwj_mapping = build_mapping({'👨': '5000000000000000001', '👨\u200d🎨': '5000000000000000005'})

    assert '5000000000000000005' in substitute_custom_emoji('👨\u200d🎨', mapping=zwj_mapping)


def test_substitution_cap_enforced(mapping):
    text = '✅' * (MAX_SUBSTITUTIONS_PER_FIELD + 10)

    result = substitute_custom_emoji(text, mapping=mapping)

    assert result.count('<tg-emoji') == MAX_SUBSTITUTIONS_PER_FIELD
    assert result.endswith('✅' * 10)


def test_empty_and_none_input(mapping):
    assert substitute_custom_emoji('', mapping=mapping) == ''
    assert substitute_custom_emoji(None, mapping=mapping) is None


def test_empty_mapping_returns_text_unchanged():
    empty = build_mapping({})

    assert substitute_custom_emoji('✅', mapping=empty) == '✅'


def test_loader_rejects_non_digit_ids(tmp_path):
    path = tmp_path / 'map.json'
    payload = {
        'map': {
            '✅': CHECK_ID,
            '❌': 'abc',
            '⚠': '123"onerror=x',
            '🔥': 12345,
            '💡': '',
            '🎯': '1' * 33,
        }
    }
    path.write_text(json.dumps(payload), encoding='utf-8')

    mapping = load_mapping(path)

    assert dict(mapping.emoji_map) == {'✅': CHECK_ID}


def test_loader_strips_vs16_from_keys(tmp_path):
    path = tmp_path / 'map.json'
    path.write_text(json.dumps({'map': {'⬅\ufe0f': LEFT_ID}}), encoding='utf-8')

    mapping = load_mapping(path)

    assert dict(mapping.emoji_map) == {'⬅': LEFT_ID}


def test_loader_missing_file_returns_empty_mapping(tmp_path):
    mapping = load_mapping(tmp_path / 'does-not-exist.json')

    assert dict(mapping.emoji_map) == {}
    assert mapping.pattern is None


def test_loader_broken_json_returns_empty_mapping(tmp_path):
    path = tmp_path / 'map.json'
    path.write_text('{not json', encoding='utf-8')

    mapping = load_mapping(path)

    assert dict(mapping.emoji_map) == {}


def test_leading_emoji_id_found(mapping):
    assert get_leading_emoji_id('✅ Готово', mapping=mapping) == CHECK_ID


def test_leading_emoji_id_with_vs16(mapping):
    """⬅️ Назад: ключ карты VS16-stripped, но искать нужно по фактическому тексту."""
    assert get_leading_emoji_id('⬅\ufe0f Назад', mapping=mapping) == LEFT_ID


def test_leading_emoji_id_without_space(mapping):
    assert get_leading_emoji_id('✅Готово', mapping=mapping) == CHECK_ID


def test_leading_emoji_id_emoji_only(mapping):
    assert get_leading_emoji_id('⬅\ufe0f', mapping=mapping) == LEFT_ID


def test_leading_emoji_id_unmapped_emoji(mapping):
    assert get_leading_emoji_id('🦄 Единорог', mapping=mapping) is None


def test_leading_emoji_id_no_emoji(mapping):
    assert get_leading_emoji_id('Далее', mapping=mapping) is None


def test_leading_emoji_id_ignores_non_leading_emoji(mapping):
    assert get_leading_emoji_id('Готово ✅', mapping=mapping) is None


def test_leading_emoji_id_empty_input(mapping):
    assert get_leading_emoji_id('', mapping=mapping) is None
    assert get_leading_emoji_id(None, mapping=mapping) is None


def test_leading_emoji_id_empty_mapping():
    assert get_leading_emoji_id('✅', mapping=build_mapping({})) is None


def test_leading_emoji_id_does_not_break_cluster():
    """👨 замаплен, но текст начинается с ZWJ-последовательности целиком — id не отдаём."""
    zwj_mapping = build_mapping({'👨': '5000000000000000001'})

    assert get_leading_emoji_id('👨\u200d🎨 Художник', mapping=zwj_mapping) is None


def test_leading_emoji_id_real_asset():
    assert get_leading_emoji_id('💰 Баланс') == get_mapping().emoji_map['💰']


def test_real_asset_loads():
    mapping = get_mapping()

    assert len(mapping.emoji_map) > 200
    assert all(value.isdigit() for value in mapping.emoji_map.values())
    assert mapping.pattern is not None


def test_get_mapping_is_cached():
    assert get_mapping() is get_mapping()


def test_set_mapping_replaces_active_mapping():
    replacement = build_mapping({'✅': '999'})

    set_mapping(replacement)

    assert get_mapping() is replacement
    assert substitute_custom_emoji('✅') == '<tg-emoji emoji-id="999">✅</tg-emoji>'
    assert get_leading_emoji_id('✅ Готово') == '999'


def test_set_mapping_is_not_overwritten_by_lazy_load():
    set_mapping(build_mapping({'✅': '999'}))

    assert get_mapping().emoji_map == {'✅': '999'}
    assert get_mapping().emoji_map == {'✅': '999'}


def test_reset_cache_restores_bundled_asset():
    set_mapping(build_mapping({'✅': '999'}))
    reset_mapping_cache()

    assert len(get_mapping().emoji_map) > 200


def test_load_aliases_real_asset():
    aliases = load_aliases()

    assert aliases['🔍'] == '🔎'
    assert aliases['🛑'] == '🔴'
    assert aliases
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in aliases.items())
    # Ключи и значения нормализованы: матчинг идёт по карте со срезанным VS16.
    assert all('\ufe0f' not in key and '\ufe0f' not in value for key, value in aliases.items())
    # Алиас на самого себя бессмысленен.
    assert all(key != value for key, value in aliases.items())
    # Цепочки запрещены: цель ищется в ПАКЕ, а не среди алиасов,
    # поэтому alias -> alias никогда бы не разрезолвился.
    assert not (set(aliases.values()) & set(aliases)), 'алиас указывает на другой алиас'
    # Ключ-алиас сам становится текстом entity: не-эмодзи ключ рушит всё сообщение.
    assert not [key for key in aliases if is_math_symbol_key(key)], 'ключ-алиас — математический символ'


def test_bundled_map_real_asset_has_no_alias_echo():
    """`map.json` — снимок, снятый ПОСЛЕ алиасов, и `load_mapping` читает его в обход `_apply_aliases`.

    Эхо alias-слоя (Sm-ключ, дублирующий чужой id) в снимке означает, что его перегенерировали
    из отравленного файла алиасов, и встроенный путь снова начнёт слать невалидные entity.
    """
    snapshot = dict(load_mapping().emoji_map)

    assert not find_alias_echoes(snapshot), 'в снимок просочилось эхо alias-слоя'
    # ↔ (U+2194) — тоже Sm, но со СВОИМ id: это законный ключ пака, его правило не трогает.
    assert '\u2194' in snapshot


def test_load_usage_real_asset():
    usage = load_usage()

    assert usage['❌'] == 2257
    assert len(usage) == 250
    assert sum(usage.values()) == 16397


def test_load_aliases_missing_file(tmp_path):
    assert load_aliases(tmp_path / 'nope.json') == {}


def test_load_usage_missing_file(tmp_path):
    assert load_usage(tmp_path / 'nope.json') == {}


def test_load_aliases_broken_json(tmp_path):
    path = tmp_path / 'aliases.json'
    path.write_text('{broken', encoding='utf-8')

    assert load_aliases(path) == {}


def test_load_usage_broken_json(tmp_path):
    path = tmp_path / 'usage.json'
    path.write_text('{broken', encoding='utf-8')

    assert load_usage(path) == {}


def test_load_usage_drops_non_integer_counts(tmp_path):
    path = tmp_path / 'usage.json'
    path.write_text('{"usage": {"✅": 5, "❌": "many", "💡": null}}', encoding='utf-8')

    assert load_usage(path) == {'✅': 5}


def test_production_breadcrumb_leaves_math_arrow_bare():
    """`🏠 → Платежи` с экрана «Настройки бота → группа» ронял ENTITY_TEXT_INVALID.

    U+2192 (Sm) — математическая стрелка без эмодзи-формы (эмодзи-стрелка — U+27A1 ➡️).
    Telegram валидирует ТЕКСТ entity, поэтому `<tg-emoji>→</tg-emoji>` отвергает всё
    сообщение целиком. Ключ приезжал в карту из `aliases.json`, а не из пака.
    """
    mapping = build_real_pack_mapping()

    result = substitute_custom_emoji('🏠 → Платежи', mapping=mapping)

    assert result is not None
    wrapped = ENTITY_RE.findall(result)
    assert '🏠' in wrapped, 'дом обязан остаться кастомным эмодзи'
    assert '→' not in wrapped, 'стрелка U+2192 не должна попадать в <tg-emoji>'
    assert result.endswith(' → Платежи')


def test_bundled_path_leaves_math_arrow_bare():
    """Тот же экран на ВСТРОЕННОЙ карте: `apply_bundled_mapping` минует `_apply_aliases`.

    Это честный шов прода: именно эту функцию зовёт `load_and_apply`, когда в БД не
    настроено ни одного пака. Чистка одного `aliases.json` этот путь НЕ лечит.
    """
    apply_bundled_mapping()

    result = substitute_custom_emoji('🏠 → Платежи')

    assert result is not None
    wrapped = ENTITY_RE.findall(result)
    assert '🏠' in wrapped, 'дом обязан остаться кастомным эмодзи'
    assert '→' not in wrapped, 'стрелка U+2192 не должна попадать в <tg-emoji>'
    assert result.endswith(' → Платежи')


def test_pack_sourced_keys_are_not_eaten_by_the_guard():
    """Ключи из паков валидны по построению: Telegram сам назначил их стикерам."""
    emoji_map = build_real_pack_mapping().emoji_map

    assert emoji_map['\u2194'] == load_mapping().emoji_map['\u2194'], '↔ пришёл из пака'
    assert emoji_map['\u2139'] == load_mapping().emoji_map['\u2139'], 'ℹ пришёл из пака'
    assert emoji_map['#\u20e3'] == load_mapping().emoji_map['#\u20e3'], '#⃣ пришёл из пака'
    assert '\u2192' not in emoji_map, '→ приезжал только через алиас'


def test_enabled_override_roundtrip():
    assert get_enabled_override() is None

    set_enabled_override(True)
    assert get_enabled_override() is True

    set_enabled_override(False)
    assert get_enabled_override() is False

    set_enabled_override(None)
    assert get_enabled_override() is None
