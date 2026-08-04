"""Тесты рантайм-хранилища алиасов кастомных эмодзи.

Алиасы жили только в `assets/custom_emoji/aliases.json`, вшитом в образ, поэтому
удаление вредного алиаса требовало правки кода, сборки CI и деплоя. Инцидент
`"→": "➡"` показал цену: `→` (U+2192) — математический символ без эмодзи-формы,
Telegram валидирует ТЕКСТ entity кастомного эмодзи и отвергает ВСЁ сообщение
целиком (ENTITY_TEXT_INVALID), а `→` встречается в текстах бота 285 раз.
Поэтому хранилище обязано уметь именно УДАЛЯТЬ, а не только добавлять.
"""

import json

import pytest

from app.services.custom_emoji_pack_service import (
    CUSTOM_EMOJI_ALIASES_KEY,
    CUSTOM_EMOJI_PACKS_KEY,
    AliasError,
    add_alias,
    build_mapping_from_packs,
    get_aliases,
    get_aliases_override,
    get_effective_aliases,
    is_aliases_customized,
    load_and_apply,
    parse_alias_pair,
    rebuild_and_apply,
    remove_alias,
    reset_aliases,
    save_aliases,
    set_aliases_override,
)
from app.utils import custom_emoji as custom_emoji_module
from app.utils.custom_emoji import build_mapping, load_aliases, substitute_custom_emoji
from tests.fixtures.custom_emoji_assets import ENTITY_RE, build_real_pack_mapping
from tests.services.test_custom_emoji_pack_service import FakeBot, pack, sticker


ARROW = '\u2192'  # → математическая стрелка (Sm), НЕ эмодзи
EMOJI_ARROW = '\u27a1'  # ➡ эмодзи-стрелка
HOUSE_ID = '5877468380125990001'
ARROW_ID = '5877468380125990242'
GLASS_ID = '5877468380125990333'


class FakeSettings:
    """Подмена слоя `system_settings`: тот же контракт, без БД."""

    def __init__(self, values: dict[str, str] | None = None):
        self.values: dict[str, str] = dict(values or {})

    async def get_setting_value(self, db, key: str) -> str | None:
        return self.values.get(key)

    async def upsert_system_setting(self, db, key: str, value, description=None) -> None:
        self.values[key] = value

    async def delete_system_setting(self, db, key: str) -> None:
        self.values.pop(key, None)

    def install(self, monkeypatch) -> None:
        module = 'app.services.custom_emoji_pack_service'
        monkeypatch.setattr(f'{module}.get_setting_value', self.get_setting_value)
        monkeypatch.setattr(f'{module}.upsert_system_setting', self.upsert_system_setting)
        monkeypatch.setattr(f'{module}.delete_system_setting', self.delete_system_setting)


@pytest.fixture(autouse=True)
def _clean_runtime_state():
    set_aliases_override(None)
    custom_emoji_module.reset_mapping_cache()
    yield
    set_aliases_override(None)
    custom_emoji_module.reset_mapping_cache()


@pytest.fixture
def settings(monkeypatch) -> FakeSettings:
    store = FakeSettings()
    store.install(monkeypatch)
    return store


def test_aliases_setting_key():
    assert CUSTOM_EMOJI_ALIASES_KEY == 'custom_emoji_aliases'


# --- отсутствующая настройка: поведение ровно как сегодня -------------------


@pytest.mark.asyncio
async def test_absent_setting_falls_back_to_the_shipped_file(settings):
    assert await get_aliases(db=object()) == load_aliases()
    assert await is_aliases_customized(db=object()) is False


@pytest.mark.asyncio
async def test_reading_aliases_never_writes_the_setting(settings):
    """Чтение не должно засевать БД: запись при чтении — побочный эффект, которого никто не ждёт."""
    await get_aliases(db=object())

    assert settings.values == {}


def test_absent_override_keeps_todays_mapping_byte_identical():
    """Без правок оператора прод-конвейер обязан давать ровно ту же карту, что и сегодня."""
    today = json.dumps(dict(build_real_pack_mapping().emoji_map), ensure_ascii=False, sort_keys=True)

    set_aliases_override(load_aliases())  # то, что startup ставит при отсутствующей настройке
    with_store = json.dumps(dict(build_real_pack_mapping().emoji_map), ensure_ascii=False, sort_keys=True)

    assert with_store == today


# --- сохранённая карта побеждает файл --------------------------------------


@pytest.mark.asyncio
async def test_stored_map_wins_over_the_file(settings):
    settings.values[CUSTOM_EMOJI_ALIASES_KEY] = json.dumps({'🔍': '🔎'}, ensure_ascii=False)

    assert await get_aliases(db=object()) == {'🔍': '🔎'}
    assert await is_aliases_customized(db=object()) is True


@pytest.mark.asyncio
async def test_stored_empty_map_wins_over_the_file(settings):
    """Оператор вправе снести ВСЕ алиасы: пустая карта — валидное состояние, а не «нет настройки»."""
    settings.values[CUSTOM_EMOJI_ALIASES_KEY] = '{}'

    assert await get_aliases(db=object()) == {}


@pytest.mark.asyncio
async def test_broken_stored_json_falls_back_to_the_file(settings):
    settings.values[CUSTOM_EMOJI_ALIASES_KEY] = '{broken'

    assert await get_aliases(db=object()) == load_aliases()


@pytest.mark.asyncio
async def test_stored_map_removes_a_shipped_alias(settings):
    """Ключевая способность: УДАЛИТЬ алиас, который приезжает из образа."""
    assert load_aliases()['🔍'] == '🔎', 'предпосылка: файл образа возит этот алиас'
    survivors = {key: value for key, value in load_aliases().items() if key != '🔍'}
    settings.values[CUSTOM_EMOJI_ALIASES_KEY] = json.dumps(survivors, ensure_ascii=False)
    set_aliases_override(await get_aliases(db=object()))

    mapping = await build_mapping_from_packs(FakeBot({'A': pack(sticker('🔎', GLASS_ID))}), ['A'])

    assert mapping['🔎'] == GLASS_ID
    assert '🔍' not in mapping, 'удалённый из хранилища алиас не должен воскресать из файла'


@pytest.mark.asyncio
async def test_stored_map_without_the_arrow_leaves_it_bare(settings):
    """Сценарий инцидента: карта без `→` не должна оборачивать стрелку в <tg-emoji>."""
    settings.values[CUSTOM_EMOJI_ALIASES_KEY] = json.dumps({'🔍': '🔎'}, ensure_ascii=False)
    set_aliases_override(await get_aliases(db=object()))
    bot = FakeBot({'A': pack(sticker('🏠', HOUSE_ID), sticker(EMOJI_ARROW, ARROW_ID))})

    mapping = build_mapping(await build_mapping_from_packs(bot, ['A']))
    result = substitute_custom_emoji('🏠 → Платежи', mapping=mapping)

    assert result is not None
    wrapped = ENTITY_RE.findall(result)
    assert '🏠' in wrapped, 'дом обязан остаться кастомным эмодзи'
    assert ARROW not in wrapped, 'стрелка U+2192 не должна попадать в <tg-emoji>'
    assert result.endswith(' → Платежи')


# --- гард Sm защищает и путь админки ---------------------------------------


@pytest.mark.asyncio
async def test_save_aliases_rejects_math_symbol_key(settings):
    with pytest.raises(AliasError) as error:
        await save_aliases(db=object(), aliases={ARROW: EMOJI_ARROW})

    assert ARROW in str(error.value)
    assert 'ENTITY_TEXT_INVALID' in str(error.value)
    assert settings.values == {}, 'отклонённая карта не должна попадать в БД'
    assert get_aliases_override() is None, 'отклонённая карта не должна попадать в кеш'


@pytest.mark.asyncio
async def test_add_alias_rejects_math_symbol_key(settings):
    with pytest.raises(AliasError) as error:
        await add_alias(db=object(), alias=ARROW, target=EMOJI_ARROW)

    assert ARROW in str(error.value)
    assert settings.values == {}


@pytest.mark.parametrize('bad_key', ['a', 'abc', 'ключ', '🔍 🔎', '\n'])
@pytest.mark.asyncio
async def test_add_alias_rejects_non_emoji_key(settings, bad_key):
    """Буква в ключе рушит КАЖДОЕ сообщение с этой буквой — это `→`, только хуже."""
    with pytest.raises(AliasError) as error:
        await save_aliases(db=object(), aliases={bad_key: '🔎'})

    assert 'ENTITY_TEXT_INVALID' in str(error.value)
    assert settings.values == {}


@pytest.mark.parametrize('good_key', ['🔍', '\u203c', '#\u20e3', '1\u20e3', '\u267b'])
@pytest.mark.asyncio
async def test_save_aliases_keeps_legitimate_emoji_keys(settings, good_key):
    """Гард не имеет права съедать законные ключи: `‼` — Po, `#⃣`/`1⃣` — keycap с Po и цифрой, `♻` — So.

    `↔` (U+2194) сюда НЕ входит намеренно: он Sm, и существующий гард отвергает его как
    ключ-алиас. Как ключ ПАКА он остаётся законным — Telegram сам назначил его стикеру.
    """
    result = await save_aliases(db=object(), aliases={good_key: '🔎'})

    assert result == {good_key: '🔎'}


@pytest.mark.asyncio
async def test_add_alias_rejects_empty_key(settings):
    with pytest.raises(AliasError):
        await add_alias(db=object(), alias='\ufe0f', target='🔎')


@pytest.mark.asyncio
async def test_add_alias_rejects_duplicate(settings):
    with pytest.raises(AliasError) as error:
        await add_alias(db=object(), alias='🔍', target='❌')

    assert '🔍' in str(error.value)
    assert settings.values == {}


@pytest.mark.asyncio
async def test_add_alias_allows_unknown_target(settings):
    """Пак может приехать позже: алиас без цели инертен, а не вреден — предупреждаем, но пускаем."""
    result = await add_alias(db=object(), alias='🦄', target='🦓')

    assert result['🦄'] == '🦓'
    assert json.loads(settings.values[CUSTOM_EMOJI_ALIASES_KEY])['🦄'] == '🦓'


@pytest.mark.asyncio
async def test_add_alias_writes_the_whole_map(settings):
    await add_alias(db=object(), alias='🦄', target='🔎')

    stored = json.loads(settings.values[CUSTOM_EMOJI_ALIASES_KEY])
    assert stored['🔍'] == '🔎', 'в хранилище лежит ПОЛНАЯ карта, иначе удаление невозможно'
    assert len(stored) == len(load_aliases()) + 1


@pytest.mark.asyncio
async def test_remove_alias_drops_the_key(settings):
    result = await remove_alias(db=object(), alias='🔍')

    assert '🔍' not in result
    assert '🔍' not in json.loads(settings.values[CUSTOM_EMOJI_ALIASES_KEY])
    assert get_effective_aliases() == result


@pytest.mark.asyncio
async def test_remove_alias_unknown_key_raises(settings):
    with pytest.raises(AliasError):
        await remove_alias(db=object(), alias='🦄')


@pytest.mark.asyncio
async def test_reset_restores_exactly_the_file(settings):
    await remove_alias(db=object(), alias='🔍')
    assert '🔍' not in get_effective_aliases()

    restored = await reset_aliases(db=object())

    assert restored == load_aliases()
    assert get_effective_aliases() == load_aliases()
    assert CUSTOM_EMOJI_ALIASES_KEY not in settings.values
    assert await is_aliases_customized(db=object()) is False


# --- кеш: правка видна на следующем рендере, без рестарта -------------------


@pytest.mark.asyncio
async def test_alias_change_is_visible_without_restart(settings):
    substituted = f'<tg-emoji emoji-id="{GLASS_ID}">🔍</tg-emoji> поиск'
    bot = FakeBot({'A': pack(sticker('🔎', GLASS_ID))})
    await rebuild_and_apply(bot, ['A'])
    assert substitute_custom_emoji('🔍 поиск') == substituted, 'предпосылка: алиас из файла работает'

    await remove_alias(db=object(), alias='🔍')
    await rebuild_and_apply(bot, ['A'])
    after_remove = substitute_custom_emoji('🔍 поиск')

    await add_alias(db=object(), alias='🔍', target='🔎')
    await rebuild_and_apply(bot, ['A'])
    after_add = substitute_custom_emoji('🔍 поиск')

    assert after_remove == '🔍 поиск', 'удаление алиаса видно на следующем рендере, без рестарта'
    assert after_add == substituted


@pytest.mark.asyncio
async def test_load_and_apply_installs_stored_aliases(settings):
    settings.values[CUSTOM_EMOJI_PACKS_KEY] = 'A'
    settings.values[CUSTOM_EMOJI_ALIASES_KEY] = json.dumps({'🦄': '🔎'}, ensure_ascii=False)
    bot = FakeBot({'A': pack(sticker('🔎', GLASS_ID))})

    await load_and_apply(bot, db=object())

    assert get_aliases_override() == {'🦄': '🔎'}
    assert custom_emoji_module.get_mapping().emoji_map['🦄'] == GLASS_ID
    assert '🔍' not in custom_emoji_module.get_mapping().emoji_map


# --- разбор ввода оператора -------------------------------------------------


@pytest.mark.parametrize(
    'raw',
    ['🔍 🔎', '🔍 -> 🔎', '🔍 → 🔎', '  🔍   🔎  ', '🔍=🔎', '🔍\ufe0f 🔎'],
)
def test_parse_alias_pair_accepted(raw):
    assert parse_alias_pair(raw) == ('🔍', '🔎')


@pytest.mark.parametrize('raw', ['🔍', '', '   ', '🔍 🔎 ❌', None, '->'])
def test_parse_alias_pair_rejected(raw):
    assert parse_alias_pair(raw) is None


# --- проводка админ-раздела -------------------------------------------------


def test_alias_admin_section_is_wired():
    from app.handlers.admin import custom_emoji as admin_custom_emoji
    from app.states import CustomEmojiStates

    assert CustomEmojiStates.waiting_for_alias_pair is not None

    callbacks = {
        getattr(handler.callback, '__name__', '') for handler in admin_custom_emoji.router.callback_query.handlers
    }
    assert {
        'show_aliases',
        'prompt_alias_pair',
        'show_alias_remove_list',
        'remove_alias_key',
        'reset_aliases_to_file',
    } <= callbacks

    keyboard = admin_custom_emoji._root_keyboard(enabled=True, packs=[], language='ru')
    root_callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert 'admin_ce:aliases' in root_callbacks, 'вход в алиасы живёт внутри раздела кастомных эмодзи'


def test_alias_remove_callback_data_survives_every_shipped_key():
    """`admin_ce:al:rm:<ключ>`: ключ обязан доезжать целиком и влезать в лимит 64 байта."""
    from app.handlers.admin import custom_emoji as admin_custom_emoji

    for key in load_aliases():
        data = f'admin_ce:al:rm:{key}'
        assert data.split(':', 3)[3] == key
        assert len(data.encode('utf-8')) <= 64
        assert not data.startswith('admin_ce:rm:'), 'не должно ловиться обработчиком удаления пака'

    assert admin_custom_emoji.ALIAS_BUTTONS_PER_ROW >= 1


def test_alias_screen_warns_when_no_packs_configured():
    """Без паков активна вшитая карта: алиасы в неё уже впечатаны, правки на неё не влияют."""
    from app.handlers.admin import custom_emoji as admin_custom_emoji

    with_packs = admin_custom_emoji._alias_text({'🔍': '🔎'}, customized=True, packs=['A'], mapping={'🔎': GLASS_ID})
    without_packs = admin_custom_emoji._alias_text({'🔍': '🔎'}, customized=False, packs=[], mapping={})

    assert 'правки оператора' in with_packs
    assert 'встроенный файл образа' in without_packs
    assert '⚠️' in without_packs and '⚠️' not in with_packs
    assert 'цели нет в карте' in without_packs
