"""Кнопка «Вернуться к оформлению подписки» живёт на экране «Баланс», а не в главном меню.

Раньше она появлялась в ГЛАВНОМ МЕНЮ, как только в Redis находилась сохранённая
корзина (её пишет только неудачная покупка из-за нехватки средств), и убрать её
пользователь не мог никак — только дождаться истечения TTL. Теперь:

  * главное меню рендерится одинаково с корзиной и без неё;
  * ``get_main_menu_keyboard_async`` не ходит в Redis (кнопки там больше нет —
    платить за запрос на каждый рендер меню незачем);
  * кнопка переехала на экран «Баланс» первым рядом — пополнение и есть то,
    зачем пользователь туда пришёл;
  * рядом стоит «✕ Отменить» (``cart_dismiss``), которая удаляет корзину.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.localization.texts import get_texts


LOCALE_DIR = Path(__file__).resolve().parents[1] / 'app' / 'localization' / 'locales'
LANGS = ('ru', 'en', 'ua', 'fa', 'zh')

CART_CALLBACKS = {'return_to_saved_cart', 'subscription_resume_checkout', 'cart_dismiss'}


def _layout(markup) -> list[list[tuple[str, str | None]]]:
    return [[(btn.text, btn.callback_data) for btn in row] for row in markup.inline_keyboard]


def _callbacks(markup) -> set[str | None]:
    return {btn.callback_data for row in markup.inline_keyboard for btn in row}


# --- 1. TTL ---------------------------------------------------------------


def test_cart_ttl_is_fifteen_minutes():
    assert settings.CART_TTL_SECONDS == 900


# --- 2. Главное меню ------------------------------------------------------


def test_main_menu_renders_identically_with_and_without_saved_cart():
    from app.keyboards.inline import get_main_menu_keyboard

    with_cart = get_main_menu_keyboard(has_saved_cart=True)
    without_cart = get_main_menu_keyboard(has_saved_cart=False)

    assert _layout(with_cart) == _layout(without_cart)
    assert not (_callbacks(with_cart) & CART_CALLBACKS)


def test_main_menu_ignores_show_resume_checkout_too():
    from app.keyboards.inline import get_main_menu_keyboard

    markup = get_main_menu_keyboard(show_resume_checkout=True)

    assert not (_callbacks(markup) & CART_CALLBACKS)


@pytest.mark.asyncio
async def test_async_main_menu_never_touches_redis(monkeypatch):
    """Кнопки в меню нет — значит и запроса в Redis быть не должно."""
    from app.keyboards import inline
    from app.services.user_cart_service import user_cart_service

    spy = AsyncMock(return_value=True)
    monkeypatch.setattr(user_cart_service, 'has_user_cart', spy)

    markup = await inline.get_main_menu_keyboard_async(
        db=AsyncMock(),
        user=SimpleNamespace(id=42, language='ru', username='u', created_at=None, promo_group_id=None),
    )

    spy.assert_not_awaited()
    assert not (_callbacks(markup) & CART_CALLBACKS)


# --- 3. Экран «Баланс» ----------------------------------------------------


def test_balance_keyboard_without_cart_is_unchanged():
    from app.keyboards.inline import get_balance_keyboard

    texts = get_texts('ru')
    markup = get_balance_keyboard('ru')

    assert _layout(markup)[0] == [
        (texts.BALANCE_HISTORY, 'balance_history'),
        (texts.BALANCE_TOP_UP, 'balance_topup'),
    ]
    assert not (_callbacks(markup) & CART_CALLBACKS)


def test_balance_keyboard_with_cart_puts_resume_and_dismiss_first():
    from app.keyboards.inline import get_balance_keyboard

    texts = get_texts('ru')
    baseline = _layout(get_balance_keyboard('ru'))
    markup = get_balance_keyboard('ru', has_saved_cart=True)
    rows = _layout(markup)

    assert rows[0] == [
        (texts.RETURN_TO_SUBSCRIPTION_CHECKOUT, 'return_to_saved_cart'),
        (texts.CART_DISMISS_BUTTON, 'cart_dismiss'),
    ]
    assert rows[1:] == baseline


# --- 4. Хендлер «Баланс» --------------------------------------------------


def _make_callback():
    message = MagicMock()
    message.text = 'старый текст'
    message.caption = None
    message.edit_text = AsyncMock()
    message.edit_caption = AsyncMock()
    message.answer = AsyncMock()

    callback = MagicMock()
    callback.message = message
    callback.answer = AsyncMock()
    return callback


def _make_user():
    user = MagicMock()
    user.id = 7
    user.language = 'ru'
    user.balance_kopeks = 12_300
    return user


def _sent_markup(callback):
    return callback.message.edit_text.await_args.kwargs['reply_markup']


@pytest.mark.asyncio
async def test_balance_screen_shows_cart_buttons_when_cart_exists(monkeypatch):
    from app.handlers.balance import main as balance_main
    from app.services.user_cart_service import user_cart_service

    monkeypatch.setattr(user_cart_service, 'has_user_cart', AsyncMock(return_value=True))

    callback = _make_callback()
    await balance_main.show_balance_menu(callback, _make_user(), AsyncMock())

    assert {'return_to_saved_cart', 'cart_dismiss'} <= _callbacks(_sent_markup(callback))


@pytest.mark.asyncio
async def test_balance_screen_hides_cart_buttons_when_no_cart(monkeypatch):
    from app.handlers.balance import main as balance_main
    from app.services.user_cart_service import user_cart_service

    monkeypatch.setattr(user_cart_service, 'has_user_cart', AsyncMock(return_value=False))

    callback = _make_callback()
    await balance_main.show_balance_menu(callback, _make_user(), AsyncMock())

    assert not (_callbacks(_sent_markup(callback)) & CART_CALLBACKS)


@pytest.mark.asyncio
async def test_balance_screen_survives_redis_failure(monkeypatch):
    """Упавший Redis не должен ломать экран «Баланс» — просто нет кнопки."""
    from app.handlers.balance import main as balance_main
    from app.services.user_cart_service import user_cart_service

    async def _explode(*_args, **_kwargs):
        raise RuntimeError('redis down')

    monkeypatch.setattr(user_cart_service, 'has_user_cart', _explode)

    callback = _make_callback()
    await balance_main.show_balance_menu(callback, _make_user(), AsyncMock())

    callback.message.edit_text.assert_awaited_once()
    assert not (_callbacks(_sent_markup(callback)) & CART_CALLBACKS)


# --- 5. Отмена корзины ----------------------------------------------------


@pytest.mark.asyncio
async def test_cart_dismiss_deletes_cart_and_rerenders_without_buttons(monkeypatch):
    from app.handlers.balance import main as balance_main
    from app.services.user_cart_service import user_cart_service

    delete = AsyncMock(return_value=True)
    monkeypatch.setattr(user_cart_service, 'delete_user_cart', delete)
    monkeypatch.setattr(user_cart_service, 'has_user_cart', AsyncMock(return_value=False))

    callback = _make_callback()
    user = _make_user()
    await balance_main.handle_cart_dismiss(callback, user, AsyncMock())

    delete.assert_awaited_once_with(user.id)
    callback.answer.assert_awaited()
    assert get_texts('ru').CART_DISMISSED_ALERT in str(callback.answer.await_args)
    assert not (_callbacks(_sent_markup(callback)) & CART_CALLBACKS)


@pytest.mark.asyncio
async def test_cart_dismiss_without_cart_is_a_noop(monkeypatch):
    from app.handlers.balance import main as balance_main
    from app.services.user_cart_service import user_cart_service

    monkeypatch.setattr(user_cart_service, 'delete_user_cart', AsyncMock(return_value=False))
    monkeypatch.setattr(user_cart_service, 'has_user_cart', AsyncMock(return_value=False))

    callback = _make_callback()
    await balance_main.handle_cart_dismiss(callback, _make_user(), AsyncMock())

    callback.message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_cart_dismiss_survives_redis_failure(monkeypatch):
    from app.handlers.balance import main as balance_main
    from app.services.user_cart_service import user_cart_service

    async def _explode(*_args, **_kwargs):
        raise RuntimeError('redis down')

    monkeypatch.setattr(user_cart_service, 'delete_user_cart', _explode)
    monkeypatch.setattr(user_cart_service, 'has_user_cart', _explode)

    callback = _make_callback()
    await balance_main.handle_cart_dismiss(callback, _make_user(), AsyncMock())

    callback.message.edit_text.assert_awaited_once()


def test_cart_dismiss_handler_is_registered():
    from app.handlers.balance.main import handle_cart_dismiss, register_balance_handlers

    dp = MagicMock()
    register_balance_handlers(dp)

    registered = [call.args[0] for call in dp.callback_query.register.call_args_list if call.args]
    assert handle_cart_dismiss in registered


# --- 6. Локали ------------------------------------------------------------


@pytest.mark.parametrize('lang', LANGS)
@pytest.mark.parametrize('key', ['CART_DISMISS_BUTTON', 'CART_DISMISSED_ALERT'])
def test_new_cart_keys_exist_in_every_locale(lang, key):
    data = json.loads((LOCALE_DIR / f'{lang}.json').read_text(encoding='utf-8'))

    assert key in data, f'{lang}.json не содержит {key}'
    assert data[key].strip(), f'{lang}.json: {key} пустой'


def test_russian_cart_key_values():
    ru = json.loads((LOCALE_DIR / 'ru.json').read_text(encoding='utf-8'))

    assert ru['CART_DISMISS_BUTTON'] == '✕ Отменить'
    assert ru['CART_DISMISSED_ALERT'] == 'Оформление отменено'
