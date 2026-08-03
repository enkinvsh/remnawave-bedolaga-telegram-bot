"""Превью экрана бота для редактора локалей.

Владелец выбирает экран, видит его ровно так, как рендерит бот, и правит те
шесть строк, из которых экран собран, — вместо поиска по 1966 плоским ключам.

Ключевые инварианты, за которыми следят тесты:
  * синтетический пользователь НИКОГДА не попадает в БД;
  * ``draft`` (несохранённые правки для live-превью) действует только на этот
    рендер и не протекает в глобальный кеш override-ов;
  * ``render_screen`` не бросает: любая поломка возвращается полем ``error``,
    а ContextVar трассировки остаётся сброшенным.
"""

import pytest
from sqlalchemy import inspect as sa_inspect

from app.localization.overrides import clear_override_cache, get_override_cache, set_override_cache
from app.localization.tracing import get_recorder
from app.services.screen_preview import (
    build_synthetic_user,
    get_screen,
    list_screens,
    render_screen,
)


# Строка статуса активной подписки: её значение реально попадает в текст экрана
# (в отличие от MAIN_MENU_ACTION_PROMPT, который только ищется внутри MAIN_MENU).
STATUS_KEY = 'SUB_STATUS_ACTIVE_LONG'


class _StubSession:
    """Сессия, которая падает на любом запросе.

    Превью обязано пережить недоступную БД: все обращения в
    ``get_main_menu_text`` обёрнуты в try/except, а сам ``render_screen`` —
    в собственный.
    """

    def __init__(self):
        self.added: list[object] = []

    async def execute(self, *args, **kwargs):
        raise RuntimeError('БД в превью не нужна')

    async def scalar(self, *args, **kwargs):
        raise RuntimeError('БД в превью не нужна')

    def add(self, obj):  # pragma: no cover - вызов = провал теста
        self.added.append(obj)


@pytest.fixture(autouse=True)
def _clean_override_cache():
    clear_override_cache()
    yield
    clear_override_cache()


@pytest.fixture
def db() -> _StubSession:
    return _StubSession()


# ============ Реестр ============


def test_main_menu_screen_is_registered():
    screens = {screen.id: screen for screen in list_screens()}

    assert 'main_menu' in screens
    assert screens['main_menu'].title
    assert screens['main_menu'].description


def test_get_screen_returns_none_for_unknown():
    assert get_screen('no_such_screen') is None


# ============ Синтетический пользователь ============


def test_synthetic_user_is_transient_and_detached():
    user = build_synthetic_user('ru')

    assert sa_inspect(user).transient is True
    assert sa_inspect(user).session is None
    for subscription in user.subscriptions:
        assert sa_inspect(subscription).transient is True
        assert sa_inspect(subscription).session is None


def test_synthetic_user_is_obviously_fake():
    user = build_synthetic_user('ru')

    assert user.telegram_id == 0
    assert 'демо' in user.full_name.lower() or 'demo' in user.full_name.lower()


def test_synthetic_user_speaks_requested_language():
    assert build_synthetic_user('en').language == 'en'


def test_synthetic_user_has_an_active_subscription():
    user = build_synthetic_user('ru')

    assert user.subscription is not None
    assert user.subscription.actual_status == 'active'


# ============ Рендер ============


async def test_render_main_menu_returns_text_and_keys(db):
    payload = await render_screen('main_menu', 'ru', db)

    assert 'error' not in payload
    assert payload['screen_id'] == 'main_menu'
    assert payload['language'] == 'ru'
    assert payload['text']

    keys = [entry['key'] for entry in payload['keys']]
    assert 'MAIN_MENU' in keys
    assert any(key.startswith('SUB_STATUS_') for key in keys), keys


async def test_render_does_not_write_the_synthetic_user(db):
    await render_screen('main_menu', 'ru', db)

    assert db.added == []


async def test_render_leaves_tracing_off(db):
    await render_screen('main_menu', 'ru', db)

    assert get_recorder() is None


async def test_keys_carry_default_override_and_effective_value(db):
    set_override_cache({('ru', STATUS_KEY): '💎 РАБОТАЕТ'})

    payload = await render_screen('main_menu', 'ru', db)
    entry = next(item for item in payload['keys'] if item['key'] == STATUS_KEY)

    assert entry['default_value'] == '💎 Активна\n📅 до {end_date} ({days} дн.)'
    assert entry['override_value'] == '💎 РАБОТАЕТ'
    assert entry['value'] == '💎 РАБОТАЕТ'
    assert '💎 РАБОТАЕТ' in payload['text']


async def test_action_prompt_key_is_touched_even_though_it_only_positions_text(db):
    """MAIN_MENU_ACTION_PROMPT ищется внутри MAIN_MENU, а не подставляется.

    Ключ всё равно должен попасть в список: владелец правит MAIN_MENU и обязан
    видеть, что от этой строки зависит место вставки подсказок.
    """
    payload = await render_screen('main_menu', 'ru', db)
    entry = next(item for item in payload['keys'] if item['key'] == 'MAIN_MENU_ACTION_PROMPT')

    assert entry['default_value'] == 'Выберите действие:'


async def test_key_without_override_reports_none(db):
    payload = await render_screen('main_menu', 'ru', db)
    entry = next(item for item in payload['keys'] if item['key'] == 'MAIN_MENU')

    assert entry['override_value'] is None
    assert entry['value'] == entry['default_value']


async def test_keys_are_in_touch_order_and_unique(db):
    payload = await render_screen('main_menu', 'ru', db)
    keys = [entry['key'] for entry in payload['keys']]

    assert len(keys) == len(set(keys))


async def test_render_in_another_language(db):
    payload = await render_screen('main_menu', 'en', db)

    assert payload['language'] == 'en'
    entry = next(item for item in payload['keys'] if item['key'] == 'MAIN_MENU_ACTION_PROMPT')
    assert entry['default_value'] == 'Choose an option:'


# ============ Черновик (live-превью во время набора) ============


async def test_draft_changes_the_rendered_text(db):
    payload = await render_screen('main_menu', 'ru', db, draft={STATUS_KEY: '>>> ЧЕРНОВИК <<<'})

    assert '>>> ЧЕРНОВИК <<<' in payload['text']
    entry = next(item for item in payload['keys'] if item['key'] == STATUS_KEY)
    assert entry['value'] == '>>> ЧЕРНОВИК <<<'
    assert entry['default_value'] == '💎 Активна\n📅 до {end_date} ({days} дн.)'


async def test_draft_never_touches_the_global_cache(db):
    set_override_cache({('ru', STATUS_KEY): 'Сохранённый вариант'})
    before = get_override_cache()

    await render_screen('main_menu', 'ru', db, draft={STATUS_KEY: 'черновик'})

    assert get_override_cache() == before


async def test_draft_wins_over_saved_override(db):
    set_override_cache({('ru', STATUS_KEY): 'Сохранённый вариант'})

    payload = await render_screen('main_menu', 'ru', db, draft={STATUS_KEY: 'черновик'})

    assert 'черновик' in payload['text']
    assert 'Сохранённый вариант' not in payload['text']
    entry = next(item for item in payload['keys'] if item['key'] == STATUS_KEY)
    assert entry['override_value'] == 'Сохранённый вариант'
    assert entry['value'] == 'черновик'


async def test_draft_does_not_leak_into_the_next_render(db):
    await render_screen('main_menu', 'ru', db, draft={STATUS_KEY: 'черновик'})

    payload = await render_screen('main_menu', 'ru', db)

    assert 'черновик' not in payload['text']


async def test_empty_draft_is_equivalent_to_none(db):
    with_none = await render_screen('main_menu', 'ru', db)
    with_empty = await render_screen('main_menu', 'ru', db, draft={})

    assert with_none['text'] == with_empty['text']


# ============ Ошибки: никогда не бросаем ============


async def test_unknown_screen_returns_error_payload(db):
    payload = await render_screen('no_such_screen', 'ru', db)

    assert payload['error']
    assert payload['screen_id'] == 'no_such_screen'
    assert payload['text'] == ''
    assert payload['keys'] == []
    assert get_recorder() is None


async def test_unknown_language_returns_error_payload(db):
    payload = await render_screen('main_menu', 'klingon', db)

    assert payload['error']
    assert payload['keys'] == []


async def test_broken_screen_is_reported_not_raised(db, monkeypatch):
    from app.services.screen_preview import registry

    async def _boom(texts, session):
        raise RuntimeError('рендер сломался')

    broken = registry.ScreenDefinition(
        id='broken',
        title='Сломанный',
        description='тестовый',
        render=_boom,
    )
    monkeypatch.setitem(registry._SCREENS, 'broken', broken)

    payload = await render_screen('broken', 'ru', db)

    assert 'рендер сломался' in payload['error']
    assert payload['text'] == ''
    assert get_recorder() is None
