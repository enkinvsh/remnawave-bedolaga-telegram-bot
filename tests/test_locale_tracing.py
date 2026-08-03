"""Опциональная запись ключей локализации, использованных при рендере экрана.

Трассировка нужна превью экранов в кабинете: чтобы показать владельцу «этот
экран собран вот из этих шести строк», надо знать, какие ключи реально
дёрнулись. ``Texts._get_value`` вызывается для каждой строки каждого
сообщения, поэтому по умолчанию запись ВЫКЛЮЧЕНА и стоит один
``ContextVar.get()``.
"""

import asyncio

import pytest

from app.localization.overrides import clear_override_cache, set_override_cache
from app.localization.texts import get_texts
from app.localization.tracing import get_recorder, record_key, trace_keys


@pytest.fixture(autouse=True)
def _clean_override_cache():
    clear_override_cache()
    yield
    clear_override_cache()


# ============ Выключено по умолчанию ============


def test_recorder_is_none_by_default():
    assert get_recorder() is None


def test_record_key_is_noop_when_disabled():
    record_key('ACCESS_DENIED')
    assert get_recorder() is None


def test_reading_texts_without_trace_does_not_record():
    texts = get_texts('ru')
    _ = texts.ACCESS_DENIED
    assert get_recorder() is None


def test_recorder_is_reset_after_context():
    with trace_keys() as keys:
        record_key('ACCESS_DENIED')
        assert get_recorder() is keys

    assert get_recorder() is None


def test_recorder_is_reset_after_exception():
    with pytest.raises(RuntimeError), trace_keys():
        record_key('ACCESS_DENIED')
        raise RuntimeError('boom')

    assert get_recorder() is None


# ============ Что именно записывается ============


def test_keys_are_captured_in_touch_order():
    texts = get_texts('ru')

    with trace_keys() as keys:
        _ = texts.ACCESS_DENIED
        _ = texts.MAIN_MENU
        _ = texts.SUB_STATUS_NONE

    assert keys == ['ACCESS_DENIED', 'MAIN_MENU', 'SUB_STATUS_NONE']


def test_keys_are_deduplicated_keeping_first_position():
    texts = get_texts('ru')

    with trace_keys() as keys:
        _ = texts.MAIN_MENU
        _ = texts.ACCESS_DENIED
        _ = texts.MAIN_MENU

    assert keys == ['MAIN_MENU', 'ACCESS_DENIED']


def test_list_is_live_inside_the_context():
    texts = get_texts('ru')

    with trace_keys() as keys:
        assert keys == []
        _ = texts.ACCESS_DENIED
        assert keys == ['ACCESS_DENIED']


def test_override_resolution_is_recorded():
    set_override_cache({('ru', 'ACCESS_DENIED'): 'ЗАПРЕЩЕНО'})
    texts = get_texts('ru')

    with trace_keys() as keys:
        assert texts.ACCESS_DENIED == 'ЗАПРЕЩЕНО'

    assert keys == ['ACCESS_DENIED']


def test_fallback_resolution_is_recorded():
    texts = get_texts('en')
    key = 'ACCESS_DENIED'
    texts._fallback_values[key] = texts._values.pop(key)

    with trace_keys() as keys:
        _ = texts[key]

    assert keys == [key]


def test_missing_key_is_not_recorded():
    texts = get_texts('ru')

    with trace_keys() as keys:
        assert texts.t('DEFINITELY_NOT_A_REAL_KEY_XYZ', 'запасной текст') == 'запасной текст'

    assert keys == []


def test_get_with_default_for_missing_key_is_not_recorded():
    texts = get_texts('ru')

    with trace_keys() as keys:
        assert texts.get('DEFINITELY_NOT_A_REAL_KEY_XYZ') is None

    assert keys == []


# ============ Изоляция: вложенность и конкурентность ============


def test_nested_trace_does_not_bleed_into_outer():
    texts = get_texts('ru')

    with trace_keys() as outer:
        _ = texts.ACCESS_DENIED
        with trace_keys() as inner:
            _ = texts.MAIN_MENU
        _ = texts.SUB_STATUS_NONE

    assert inner == ['MAIN_MENU']
    assert outer == ['ACCESS_DENIED', 'SUB_STATUS_NONE']


async def test_concurrent_traces_do_not_bleed():
    texts = get_texts('ru')

    async def render(key: str) -> list[str]:
        with trace_keys() as keys:
            _ = texts[key]
            await asyncio.sleep(0)
            _ = texts.ACCESS_DENIED
            return list(keys)

    first, second = await asyncio.gather(render('MAIN_MENU'), render('SUB_STATUS_NONE'))

    assert first == ['MAIN_MENU', 'ACCESS_DENIED']
    assert second == ['SUB_STATUS_NONE', 'ACCESS_DENIED']
    assert get_recorder() is None


async def test_untraced_task_alongside_traced_one_records_nothing():
    texts = get_texts('ru')

    async def traced() -> list[str]:
        with trace_keys() as keys:
            await asyncio.sleep(0)
            _ = texts.MAIN_MENU
            return list(keys)

    async def untraced() -> object:
        await asyncio.sleep(0)
        _ = texts.ACCESS_DENIED
        return get_recorder()

    keys, recorder = await asyncio.gather(traced(), untraced())

    assert keys == ['MAIN_MENU']
    assert recorder is None
