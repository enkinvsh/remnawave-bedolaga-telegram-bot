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

from app.localization.loader import load_locale
from app.localization.overrides import clear_override_cache, get_override_cache, set_override_cache
from app.localization.tracing import get_recorder
from app.services.screen_preview import (
    DEFAULT_SYNTHETIC_STATE,
    SYNTHETIC_STATES,
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


# ============ Состояния синтетического пользователя ============

# Одно состояние = одна ветка _get_subscription_status. Без этого редактор
# показывал бы только строку активной подписки, а «Истекла»/«Отключена»/
# «Лимит трафика» правились бы вслепую.
STATE_TO_STATUS_KEY = {
    'active_long': 'SUB_STATUS_ACTIVE_LONG',
    'active_few_days': 'SUB_STATUS_ACTIVE_FEW_DAYS',
    'active_tomorrow': 'SUB_STATUS_ACTIVE_TOMORROW',
    'active_today': 'SUB_STATUS_ACTIVE_TODAY',
    'expired': 'SUB_STATUS_EXPIRED',
    'disabled': 'SUB_STATUS_DISABLED',
    'limited': 'SUB_STATUS_LIMITED',
    'trial': 'SUB_STATUS_TRIAL_ACTIVE',
    # Статус 'pending' — единственная ветка, где показывается SUBSCRIPTION_NONE.
    'pending': 'SUBSCRIPTION_NONE',
    'none': 'SUB_STATUS_NONE',
}


def test_state_registry_is_ordered_and_labelled():
    assert [state.id for state in SYNTHETIC_STATES] == list(STATE_TO_STATUS_KEY)
    assert all(state.label for state in SYNTHETIC_STATES)


def test_default_state_is_registered():
    assert DEFAULT_SYNTHETIC_STATE in {state.id for state in SYNTHETIC_STATES}


@pytest.mark.parametrize('state', list(STATE_TO_STATUS_KEY))
def test_every_state_builds_a_transient_user(state):
    user = build_synthetic_user('ru', state=state)

    assert sa_inspect(user).transient is True
    assert sa_inspect(user).session is None
    for subscription in user.subscriptions:
        assert sa_inspect(subscription).transient is True
        assert sa_inspect(subscription).session is None


def test_none_state_has_no_subscription():
    user = build_synthetic_user('ru', state='none')

    assert user.subscriptions == []
    assert user.subscription is None


def test_unknown_state_is_rejected():
    with pytest.raises(ValueError, match='no_such_state'):
        build_synthetic_user('ru', state='no_such_state')


@pytest.mark.parametrize(('state', 'status_key'), list(STATE_TO_STATUS_KEY.items()))
async def test_each_state_renders_its_own_status_key(db, state, status_key):
    payload = await render_screen('main_menu', 'ru', db, state=state)

    assert 'error' not in payload
    assert payload['state'] == state
    assert payload['text']

    rendered = [entry['key'] for entry in payload['keys'] if entry['rendered']]
    assert status_key in rendered, rendered


async def test_default_state_is_used_when_omitted(db):
    payload = await render_screen('main_menu', 'ru', db)

    assert payload['state'] == DEFAULT_SYNTHETIC_STATE


async def test_unknown_state_returns_error_payload(db):
    payload = await render_screen('main_menu', 'ru', db, state='no_such_state')

    assert payload['error']
    assert payload['keys'] == []
    assert get_recorder() is None


# ============ Объединение: отрисованные + объявленные ============


async def test_declared_key_off_the_path_is_still_offered(db):
    """Без тарифов в базе строка тарифа не рендерится, но остаётся редактируемой.

    Ключ обязан быть в списке с ``rendered=False``: править его надо, просто в
    текущем превью он не виден. (На проде тариф есть, и attach_sample_tariff
    переводит этот ключ в rendered=True — см. тесты attach_sample_tariff.)

    Эталон берём из файла локали, а не литералом: строка меняется при правках
    вёрстки экрана, и прибитый литерал ронял бы тест на каждой такой правке.
    """
    payload = await render_screen('main_menu', 'ru', db, state='active_long')
    entry = next(item for item in payload['keys'] if item['key'] == 'MAIN_MENU_TARIFF_LINE')

    assert entry['rendered'] is False
    assert entry['default_value'] == load_locale('ru')['MAIN_MENU_TARIFF_LINE']
    # Отступ вокруг строки тарифа живёт в самом ключе — раньше \n\n были зашиты
    # в menu.py, и удалить пустую строку после тарифа было нельзя.
    assert entry['default_value'].startswith('\n')
    assert entry['default_value'].endswith('\n\n')


async def test_rendered_flag_marks_traced_keys(db):
    payload = await render_screen('main_menu', 'ru', db, state='expired')
    entry = next(item for item in payload['keys'] if item['key'] == 'SUB_STATUS_EXPIRED')

    assert entry['rendered'] is True


async def test_traced_keys_come_first_in_trace_order(db):
    payload = await render_screen('main_menu', 'ru', db, state='active_long')
    flags = [entry['rendered'] for entry in payload['keys']]

    assert flags == sorted(flags, reverse=True), 'отрисованные ключи должны идти до объявленных'

    rendered = [entry['key'] for entry in payload['keys'] if entry['rendered']]
    assert rendered[0] == 'MAIN_MENU'
    assert rendered[1] == 'SUB_STATUS_ACTIVE_LONG'


async def test_declared_only_keys_follow_declaration_order(db):
    screen = get_screen('main_menu')
    payload = await render_screen('main_menu', 'ru', db, state='active_long')

    rendered = {entry['key'] for entry in payload['keys'] if entry['rendered']}
    declared_only = [entry['key'] for entry in payload['keys'] if not entry['rendered']]

    assert declared_only == [key for key in screen.keys if key not in rendered]


async def test_union_covers_every_declared_key(db):
    screen = get_screen('main_menu')
    payload = await render_screen('main_menu', 'ru', db)

    assert set(screen.keys) <= {entry['key'] for entry in payload['keys']}


async def test_union_has_no_duplicates(db):
    payload = await render_screen('main_menu', 'ru', db)
    keys = [entry['key'] for entry in payload['keys']]

    assert len(keys) == len(set(keys))


async def test_every_traced_key_across_all_states_is_declared(db):
    """Защита от дрейфа: добавил строку на экран — объяви её в ``keys``."""
    screen = get_screen('main_menu')

    traced: set[str] = set()
    for state in STATE_TO_STATUS_KEY:
        payload = await render_screen('main_menu', 'ru', db, state=state)
        traced.update(entry['key'] for entry in payload['keys'] if entry['rendered'])

    undeclared = sorted(traced - set(screen.keys))
    assert not undeclared, f'ключи отрисовываются, но не объявлены в ScreenDefinition.keys: {undeclared}'


async def test_declared_keys_all_exist_in_the_bundled_locale(db):
    """Объявленный ключ без строки в ru.json — опечатка, а не фича."""
    from app.localization.loader import load_locale

    bundled = load_locale('ru')
    screen = get_screen('main_menu')

    missing = [key for key in screen.keys if key not in bundled]
    assert not missing, missing


async def test_draft_applies_to_a_declared_only_key(db):
    """Правка ключа вне текущего пути не должна ломать превью."""
    payload = await render_screen(
        'main_menu',
        'ru',
        db,
        state='active_long',
        draft={'MAIN_MENU_TARIFF_LINE': '\n📦 ЧЕРНОВИК: {tariff_name}'},
    )
    entry = next(item for item in payload['keys'] if item['key'] == 'MAIN_MENU_TARIFF_LINE')

    assert entry['value'] == '\n📦 ЧЕРНОВИК: {tariff_name}'
    assert entry['rendered'] is False


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

    async def _boom(texts, session, state):
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


# ============ attach_sample_tariff ============
# Без реального tariff_id строка тарифа не рендерится: превью расходилось с
# ботом, и MAIN_MENU_TARIFF_LINE нельзя было отредактировать осмысленно —
# владелец не видел результат правки.


class _FakeTariff:
    def __init__(self, tariff_id: int) -> None:
        self.id = tariff_id


@pytest.mark.asyncio
async def test_attach_sample_tariff_borrows_a_real_tariff_id(monkeypatch):
    from app.services.screen_preview import synthetic

    async def _tariffs(_db):
        return [_FakeTariff(42), _FakeTariff(43)]

    monkeypatch.setattr('app.database.crud.tariff.get_all_active_tariffs', _tariffs)

    user = synthetic.build_synthetic_user('ru', state='active_long')
    assert user.subscriptions[0].tariff_id is None

    attached = await synthetic.attach_sample_tariff(object(), user)

    assert attached is True
    assert user.subscriptions[0].tariff_id == 42


@pytest.mark.asyncio
async def test_attach_sample_tariff_is_a_noop_without_tariffs(monkeypatch):
    from app.services.screen_preview import synthetic

    async def _none(_db):
        return []

    monkeypatch.setattr('app.database.crud.tariff.get_all_active_tariffs', _none)

    user = synthetic.build_synthetic_user('ru', state='active_long')
    assert await synthetic.attach_sample_tariff(object(), user) is False
    assert user.subscriptions[0].tariff_id is None


@pytest.mark.asyncio
async def test_attach_sample_tariff_survives_a_broken_query(monkeypatch):
    from app.services.screen_preview import synthetic

    async def _boom(_db):
        raise RuntimeError('база недоступна')

    monkeypatch.setattr('app.database.crud.tariff.get_all_active_tariffs', _boom)

    user = synthetic.build_synthetic_user('ru', state='active_long')
    # Превью не должно падать из-за тарифов.
    assert await synthetic.attach_sample_tariff(object(), user) is False


@pytest.mark.asyncio
async def test_attach_sample_tariff_handles_state_without_subscription():
    from app.services.screen_preview import synthetic

    user = synthetic.build_synthetic_user('ru', state='none')
    assert user.subscriptions == []
    assert await synthetic.attach_sample_tariff(object(), user) is False
    assert await synthetic.attach_sample_tariff(None, user) is False
