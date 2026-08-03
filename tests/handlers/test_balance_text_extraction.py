"""Текст экрана «Баланс» должен собираться одной функцией.

Хендлер принимает ``CallbackQuery`` и headless не вызывается, поэтому превью
экрана не могло бы его дёрнуть. Копировать форматирование в рендерер нельзя —
копия разъедется с ботом. Значит, форматирование живёт в
``get_balance_text(user, texts)``, а хендлер её вызывает.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.balance.main import get_balance_text, show_balance_menu
from app.localization.overrides import clear_override_cache, set_override_cache
from app.localization.texts import get_texts


@pytest.fixture(autouse=True)
def _clean_override_cache():
    clear_override_cache()
    yield
    clear_override_cache()


@pytest.fixture
def texts():
    return get_texts('ru')


def test_balance_text_matches_the_template(texts):
    user = SimpleNamespace(balance_kopeks=125_000, language='ru')

    assert get_balance_text(user, texts) == texts.BALANCE_INFO.format(balance=texts.format_price(125_000))


def test_balance_text_is_overridable(texts):
    set_override_cache({('ru', 'BALANCE_INFO'): 'На счету {balance}'})
    user = SimpleNamespace(balance_kopeks=50_000, language='ru')

    assert get_balance_text(user, texts) == f'На счету {texts.format_price(50_000)}'


def test_zero_balance_renders(texts):
    user = SimpleNamespace(balance_kopeks=0, language='ru')

    assert texts.format_price(0) in get_balance_text(user, texts)


async def test_handler_sends_exactly_get_balance_text(texts):
    """Хендлер и превью обязаны выдавать одну и ту же строку."""
    user = MagicMock()
    user.balance_kopeks = 77_700
    user.language = 'ru'

    message = MagicMock()
    message.text = 'старый текст'
    message.caption = None
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()

    callback = MagicMock()
    callback.message = message
    callback.answer = AsyncMock()

    await show_balance_menu(callback, user, AsyncMock())

    message.edit_text.assert_awaited_once()
    sent_text = message.edit_text.await_args.args[0]
    assert sent_text == get_balance_text(user, texts)
