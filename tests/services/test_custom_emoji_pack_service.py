"""Тесты сервиса рантайм-настройки паков кастомных эмодзи."""

from types import SimpleNamespace

import pytest

from app.services.custom_emoji_pack_service import (
    CUSTOM_EMOJI_ENABLED_KEY,
    CUSTOM_EMOJI_PACKS_KEY,
    PackLinkError,
    PackNotFoundError,
    PackTypeError,
    build_mapping_from_packs,
    compute_coverage,
    fetch_pack,
    load_and_apply,
    parse_pack_link,
)
from app.utils import custom_emoji as custom_emoji_module


CHECK_ID = '5897607722295103204'
CROSS_ID = '5895462764903090781'


def sticker(emoji: str, custom_emoji_id: str) -> SimpleNamespace:
    return SimpleNamespace(emoji=emoji, custom_emoji_id=custom_emoji_id)


def pack(*stickers: SimpleNamespace, sticker_type: str = 'custom_emoji') -> SimpleNamespace:
    return SimpleNamespace(sticker_type=sticker_type, stickers=list(stickers))


class FakeBot:
    def __init__(self, packs: dict[str, SimpleNamespace]):
        self._packs = packs
        self.requested: list[str] = []

    async def get_sticker_set(self, name: str) -> SimpleNamespace:
        self.requested.append(name)
        if name not in self._packs:
            raise RuntimeError(f'STICKERSET_INVALID: {name}')
        return self._packs[name]


@pytest.fixture(autouse=True)
def _clean_cache():
    custom_emoji_module.reset_mapping_cache()
    yield
    custom_emoji_module.reset_mapping_cache()


def test_settings_keys():
    assert CUSTOM_EMOJI_PACKS_KEY == 'custom_emoji_packs'
    assert CUSTOM_EMOJI_ENABLED_KEY == 'custom_emoji_enabled'


@pytest.mark.parametrize(
    'raw',
    [
        'https://t.me/addemoji/TgAndroidIcons',
        'http://t.me/addemoji/TgAndroidIcons',
        't.me/addemoji/TgAndroidIcons',
        'tg://addemoji?set=TgAndroidIcons',
        'TgAndroidIcons',
        '  https://t.me/addemoji/TgAndroidIcons  ',
    ],
)
def test_parse_pack_link_accepted(raw):
    assert parse_pack_link(raw) == 'TgAndroidIcons'


@pytest.mark.parametrize(
    'raw',
    [
        'https://t.me/joinchat/x',
        '',
        '   ',
        'bad name!',
        'a' * 65,
        'https://t.me/addemoji/',
        'https://example.com/addemoji/Name',
        None,
    ],
)
def test_parse_pack_link_rejected(raw):
    assert parse_pack_link(raw) is None


@pytest.mark.asyncio
async def test_fetch_pack_returns_pairs():
    bot = FakeBot({'P': pack(sticker('✅', CHECK_ID), sticker('❌', CROSS_ID))})

    assert await fetch_pack(bot, 'P') == [('✅', CHECK_ID), ('❌', CROSS_ID)]


@pytest.mark.asyncio
async def test_fetch_pack_missing_set():
    bot = FakeBot({})

    with pytest.raises(PackNotFoundError):
        await fetch_pack(bot, 'Nope')


@pytest.mark.asyncio
async def test_fetch_pack_rejects_regular_sticker_set():
    bot = FakeBot({'P': pack(sticker('✅', CHECK_ID), sticker_type='regular')})

    with pytest.raises(PackTypeError):
        await fetch_pack(bot, 'P')


@pytest.mark.asyncio
async def test_fetch_pack_rejects_bad_link():
    bot = FakeBot({})

    with pytest.raises(PackLinkError):
        await fetch_pack(bot, 'bad name!')


@pytest.mark.asyncio
async def test_build_mapping_first_pack_wins():
    bot = FakeBot(
        {
            'A': pack(sticker('✅', '111')),
            'B': pack(sticker('✅', '222'), sticker('❌', '333')),
        }
    )

    mapping = await build_mapping_from_packs(bot, ['A', 'B'])

    assert mapping['✅'] == '111'
    assert mapping['❌'] == '333'
    assert bot.requested == ['A', 'B']


@pytest.mark.asyncio
async def test_build_mapping_strips_vs16_and_drops_invalid_ids():
    bot = FakeBot(
        {
            'A': pack(
                sticker('⬅\ufe0f', '444'),
                sticker('❌', 'not-a-number'),
                sticker('💡', ''),
                sticker('🎯', '1' * 33),
                sticker('', '555'),
            )
        }
    )

    mapping = await build_mapping_from_packs(bot, ['A'])

    assert mapping == {'⬅': '444'}


@pytest.mark.asyncio
async def test_build_mapping_applies_aliases_when_target_present():
    """Алиас 🔍 -> 🔎: если цель в паке есть, алиас получает её id."""
    bot = FakeBot({'A': pack(sticker('🔎', '666'))})

    mapping = await build_mapping_from_packs(bot, ['A'])

    assert mapping['🔎'] == '666'
    assert mapping['🔍'] == '666'


@pytest.mark.asyncio
async def test_build_mapping_skips_alias_without_target():
    bot = FakeBot({'A': pack(sticker('✅', CHECK_ID))})

    mapping = await build_mapping_from_packs(bot, ['A'])

    assert '🔍' not in mapping


@pytest.mark.asyncio
async def test_build_mapping_alias_never_overrides_direct_hit():
    """💸 -> 💰 алиас, но если 💸 есть в паке напрямую — побеждает пак."""
    bot = FakeBot({'A': pack(sticker('💸', '777'), sticker('💰', '888'))})

    mapping = await build_mapping_from_packs(bot, ['A'])

    assert mapping['💸'] == '777'


@pytest.mark.asyncio
async def test_build_mapping_skips_unavailable_pack():
    bot = FakeBot({'A': pack(sticker('✅', CHECK_ID))})

    mapping = await build_mapping_from_packs(bot, ['Nope', 'A'])

    assert mapping['✅'] == CHECK_ID


def test_compute_coverage_shape():
    coverage = compute_coverage({'❌': CROSS_ID, '✅': CHECK_ID})

    assert coverage['unique_hit'] == 2
    assert coverage['unique_total'] == 250
    assert coverage['occ_total'] == 16397
    assert coverage['occ_hit'] == 2257 + 1587
    assert coverage['percent'] == round(100 * (2257 + 1587) / 16397)


def test_compute_coverage_empty_mapping():
    coverage = compute_coverage({})

    assert coverage['occ_hit'] == 0
    assert coverage['percent'] == 0


def test_compute_coverage_ignores_unknown_emoji():
    coverage = compute_coverage({'🦄': '999'})

    assert coverage['unique_hit'] == 0
    assert coverage['percent'] == 0


@pytest.mark.asyncio
async def test_load_and_apply_falls_back_to_bundled_asset(monkeypatch):
    monkeypatch.setattr(
        'app.services.custom_emoji_pack_service.get_setting_value',
        _fake_setting({}),
    )
    bot = FakeBot({})

    summary = await load_and_apply(bot, db=object())

    assert summary['source'] == 'bundled'
    assert summary['packs'] == []
    assert summary['count'] > 200
    assert bot.requested == []
    assert custom_emoji_module.get_mapping().emoji_map['✅']


@pytest.mark.asyncio
async def test_load_and_apply_uses_db_packs(monkeypatch):
    monkeypatch.setattr(
        'app.services.custom_emoji_pack_service.get_setting_value',
        _fake_setting({CUSTOM_EMOJI_PACKS_KEY: 'A'}),
    )
    bot = FakeBot({'A': pack(sticker('✅', '111'))})

    summary = await load_and_apply(bot, db=object())

    assert summary['source'] == 'packs'
    assert summary['packs'] == ['A']
    assert custom_emoji_module.get_mapping().emoji_map['✅'] == '111'


@pytest.mark.asyncio
async def test_load_and_apply_keeps_previous_mapping_on_failure(monkeypatch):
    custom_emoji_module.set_mapping(custom_emoji_module.build_mapping({'✅': '999'}))
    monkeypatch.setattr(
        'app.services.custom_emoji_pack_service.get_setting_value',
        _fake_setting({CUSTOM_EMOJI_PACKS_KEY: 'A'}),
    )

    class ExplodingBot:
        async def get_sticker_set(self, name):
            raise RuntimeError('network down')

    summary = await load_and_apply(ExplodingBot(), db=object())

    assert summary['source'] == 'unchanged'
    assert custom_emoji_module.get_mapping().emoji_map['✅'] == '999'


@pytest.mark.asyncio
async def test_load_and_apply_never_raises(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('db down')

    monkeypatch.setattr('app.services.custom_emoji_pack_service.get_setting_value', boom)

    summary = await load_and_apply(FakeBot({}), db=object())

    assert summary['source'] == 'unchanged'


@pytest.mark.asyncio
async def test_load_and_apply_syncs_enabled_override(monkeypatch):
    monkeypatch.setattr(
        'app.services.custom_emoji_pack_service.get_setting_value',
        _fake_setting({CUSTOM_EMOJI_ENABLED_KEY: '1'}),
    )

    await load_and_apply(FakeBot({}), db=object())

    assert custom_emoji_module.get_enabled_override() is True


def test_admin_section_is_wired_into_the_bot():
    from pathlib import Path

    from app.handlers.admin import custom_emoji as admin_custom_emoji
    from app.keyboards.admin import get_admin_settings_submenu_keyboard
    from app.states import CustomEmojiStates

    assert CustomEmojiStates.waiting_for_pack_link is not None
    assert callable(admin_custom_emoji.register_handlers)

    keyboard = get_admin_settings_submenu_keyboard('ru')
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert 'admin_custom_emoji' in callbacks

    bot_source = Path('app/bot.py').read_text(encoding='utf-8')
    assert 'admin_custom_emoji.register_handlers(dp)' in bot_source
    assert 'load_and_apply(bot, db)' in bot_source


def _fake_setting(values: dict[str, str]):
    async def _get_setting_value(db, key):
        return values.get(key)

    return _get_setting_value
