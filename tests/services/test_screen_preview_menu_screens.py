"""Превью экранов «Промокод», «Партнерка», «Инфо» и «Язык».

Главный инвариант здесь один и он же самый дорогой: экран обязан быть подключён
к ТОЙ функции, которой его рисует бот, а не к похоже названной. Ровно на этом
уже обожглись — «Подписка» смотрела в ``pricing.get_subscription_info_text``,
пока живой экран рисовал ``purchase.show_subscription_info``; владелец правил
ключи, которых экран не читает, и правки «не срабатывали».

Поэтому каждый экран здесь проверяется парой:
  * превью совпадает с тем, что собирает функция, которую зовёт хендлер;
  * правка ключа через ``draft`` реально меняет текст превью.
"""

import pytest

from app.localization.texts import get_texts
from app.services.screen_preview import get_screen, render_screen
from app.services.screen_preview.synthetic import DEFAULT_SYNTHETIC_STATE, build_synthetic_user


NEW_SCREEN_IDS = ('promocode', 'referral', 'info', 'language')


class _EmptySession:
    """Сессия, отвечающая пустым результатом — как реальная БД без строк."""

    def __init__(self):
        self.added: list[object] = []

    async def execute(self, *args, **kwargs):
        class _Result:
            def scalars(self):
                return self

            def all(self):
                return []

            def scalar(self):
                return 0

            def scalar_one_or_none(self):
                return None

            def __iter__(self):
                return iter(())

        return _Result()

    async def scalar(self, *args, **kwargs):
        return None

    def add(self, obj):  # pragma: no cover - вызов = провал теста
        self.added.append(obj)


@pytest.fixture
def db() -> _EmptySession:
    return _EmptySession()


# ============ Реестр ============


@pytest.mark.parametrize('screen_id', NEW_SCREEN_IDS)
def test_screen_is_registered_with_title_and_description(screen_id):
    screen = get_screen(screen_id)

    assert screen is not None
    assert screen.title and screen.description


@pytest.mark.parametrize('screen_id', NEW_SCREEN_IDS)
def test_screen_offers_only_one_state(screen_id):
    """Ни один из четырёх экранов не зависит от подписки."""
    screen = get_screen(screen_id)

    assert len(screen.states) == 1
    assert screen.default_state == screen.states[0].id == DEFAULT_SYNTHETIC_STATE


@pytest.mark.parametrize('screen_id', NEW_SCREEN_IDS)
async def test_screen_renders_without_error(db, screen_id):
    payload = await render_screen(screen_id, 'ru', db)

    assert 'error' not in payload, payload.get('error')
    assert payload['text']


@pytest.mark.parametrize('screen_id', NEW_SCREEN_IDS)
async def test_screen_never_writes_the_synthetic_user(db, screen_id):
    await render_screen(screen_id, 'ru', db)

    assert db.added == []


# ============ Промокод (callback menu_promocode) ============


async def test_promocode_screen_renders_the_key_the_handler_sends(db):
    """show_promocode_menu кладёт в edit_text ровно texts.PROMOCODE_ENTER."""
    payload = await render_screen('promocode', 'ru', db)

    assert payload['text'] == get_texts('ru').PROMOCODE_ENTER

    rendered = [entry['key'] for entry in payload['keys'] if entry['rendered']]
    assert rendered == ['PROMOCODE_ENTER']


async def test_promocode_screen_draft_applies(db):
    payload = await render_screen('promocode', 'ru', db, draft={'PROMOCODE_ENTER': 'ВВЕДИТЕ КОД'})

    assert payload['text'] == 'ВВЕДИТЕ КОД'


# ============ Партнерка (callback menu_referrals) ============


async def test_referral_screen_matches_the_handler_builder(db):
    """Превью и хендлер обязаны собирать текст одной и той же функцией."""
    from app.handlers.referral import build_referral_info_text

    payload = await render_screen('referral', 'ru', db)
    expected = await build_referral_info_text(build_synthetic_user('ru'), get_texts('ru'), db)

    assert payload['text'] == expected


async def test_referral_screen_traces_its_stats_block(db):
    payload = await render_screen('referral', 'ru', db)
    rendered = [entry['key'] for entry in payload['keys'] if entry['rendered']]

    assert 'REFERRAL_PROGRAM_TITLE' in rendered
    assert 'REFERRAL_STATS_HEADER' in rendered
    assert 'REFERRAL_INVITE_FOOTER' in rendered


async def test_referral_screen_shows_the_demo_code(db):
    """Билдер печатает код пользователя — у демо-пользователя он есть."""
    payload = await render_screen('referral', 'ru', db)

    assert build_synthetic_user('ru').referral_code in payload['text']


async def test_referral_screen_draft_applies(db):
    payload = await render_screen('referral', 'ru', db, draft={'REFERRAL_PROGRAM_TITLE': 'ЗАРАБОТОК'})

    assert payload['text'].startswith('ЗАРАБОТОК')


async def test_referral_screen_offers_the_db_only_keys_for_editing(db):
    """Блоки начислений недостижимы демо-пользователем, но править их надо."""
    payload = await render_screen('referral', 'ru', db)
    entries = {entry['key']: entry for entry in payload['keys']}

    for key in ('REFERRAL_RECENT_EARNINGS_ITEM', 'REFERRAL_EARNINGS_BY_TYPE_HEADER'):
        assert key in entries, key
        assert entries[key]['rendered'] is False


# ============ Инфо (callback menu_info) ============


async def test_info_screen_matches_the_handler_builder(db):
    from app.handlers.menu import build_info_menu_caption

    payload = await render_screen('info', 'ru', db)

    assert payload['text'] == build_info_menu_caption(get_texts('ru'))


async def test_info_screen_traces_both_of_its_keys(db):
    payload = await render_screen('info', 'ru', db)
    rendered = [entry['key'] for entry in payload['keys'] if entry['rendered']]

    assert set(rendered) == {'MENU_INFO_HEADER', 'MENU_INFO_PROMPT'}


async def test_info_screen_draft_applies(db):
    payload = await render_screen('info', 'ru', db, draft={'MENU_INFO_PROMPT': 'Что интересует?'})

    assert payload['text'].endswith('Что интересует?')


# ============ Язык (callback menu_language) ============


async def test_language_screen_renders_the_key_the_handler_sends(db):
    """show_language_menu кладёт в caption ровно LANGUAGE_PROMPT."""
    payload = await render_screen('language', 'ru', db)

    assert payload['text'] == get_texts('ru').t('LANGUAGE_PROMPT', '')

    rendered = [entry['key'] for entry in payload['keys'] if entry['rendered']]
    assert rendered == ['LANGUAGE_PROMPT']


async def test_language_screen_draft_applies(db):
    payload = await render_screen('language', 'ru', db, draft={'LANGUAGE_PROMPT': 'Выбери язык'})

    assert payload['text'] == 'Выбери язык'


# ============ Ни один экран не подключён к чужой функции ============


async def test_no_new_screen_renders_the_dead_subscription_info_key(db):
    """Регресс на ту самую ошибку: экран, читающий чужой ключ, бесполезен."""
    for screen_id in NEW_SCREEN_IDS:
        payload = await render_screen(screen_id, 'ru', db)
        rendered = {entry['key'] for entry in payload['keys'] if entry['rendered']}

        assert 'SUBSCRIPTION_INFO' not in rendered, screen_id
        assert 'MAIN_MENU' not in rendered, screen_id
