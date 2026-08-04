"""Экран «Поддержка»: единственный источник текста.

Текст поддержки жил в ДВУХ хранилищах сразу. Legacy — JSON-файл
``data/support_settings.json`` за ``SupportSettingsService``; новое — таблица
``locale_overrides``, которую правит редактор локалей в кабинете. Хендлер читал
JSON, поэтому правка ключа ``SUPPORT_INFO`` в кабинете «не срабатывала»: её
молча перебивал файл, и владелец видел старый текст.

Тесты прибивают ловушку: хендлер рендерит ``texts.SUPPORT_INFO``, override из
редактора выигрывает, а превью экрана отдаёт ровно ту же строку, что уходит в
Telegram, — чтобы два хранилища не отросли заново.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.handlers.admin import support_settings as admin_support
from app.handlers.support import show_support_info
from app.localization.overrides import clear_override_cache, set_override_cache
from app.localization.texts import get_texts
from app.services.screen_preview.registry import get_screen
from app.services.support_settings_service import SupportSettingsService


@pytest.fixture(autouse=True)
def _clean_override_cache():
    clear_override_cache()
    yield
    clear_override_cache()


def _callback() -> MagicMock:
    callback = MagicMock()
    callback.answer = AsyncMock()
    return callback


def _user(language: str) -> MagicMock:
    user = MagicMock()
    user.language = language
    return user


async def _sent_caption(monkeypatch, language: str) -> str:
    """Вызвать хендлер и вернуть caption, ушедший в Telegram."""
    photo = AsyncMock()
    monkeypatch.setattr('app.handlers.support.edit_or_answer_photo', photo)

    await show_support_info(_callback(), _user(language))

    return photo.await_args.kwargs['caption']


@pytest.mark.parametrize('language', ['ru', 'ua'])
async def test_handler_renders_the_localization_key(monkeypatch, language):
    """Хендлер берёт текст из локализации, а не из legacy-JSON."""
    assert await _sent_caption(monkeypatch, language) == get_texts(language).SUPPORT_INFO


async def test_locale_editor_override_wins(monkeypatch):
    """Регрессия, ради которой всё затевалось: правка в кабинете доезжает до юзера.

    Legacy-хранилище намеренно заполнено СВОИМ текстом — ровно так выглядел
    прод, где владелец правил ``SUPPORT_INFO`` в кабинете, а юзер продолжал
    видеть строку из JSON-файла. На пустом legacy-хранилище (в чекауте файла
    нет) тест был бы зелёным и при старом баге, то есть не проверял бы ничего.
    """
    monkeypatch.setattr(SupportSettingsService, '_data', {'support_info_texts': {'ru': 'ТЕКСТ ИЗ LEGACY-ФАЙЛА'}})
    monkeypatch.setattr(SupportSettingsService, '_loaded', True)
    set_override_cache({('ru', 'SUPPORT_INFO'): 'ПЕРЕОПРЕДЕЛЁННЫЙ ТЕКСТ'})

    assert await _sent_caption(monkeypatch, 'ru') == 'ПЕРЕОПРЕДЕЛЁННЫЙ ТЕКСТ'


async def test_preview_screen_matches_the_handler(monkeypatch):
    """Превью экрана показывает ровно то, что бот отправляет в caption."""
    caption = await _sent_caption(monkeypatch, 'ru')

    screen = get_screen('support')

    assert screen is not None
    assert screen.keys == ('SUPPORT_INFO',)
    assert await screen.render(get_texts('ru'), None, screen.default_state) == caption


# ============ Админская правка описания в боте ============
# Удалённый set_support_info_text хранил пустую строку безопасно: геттер видел
# blank и отдавал бандловый дефолт. У override такой поблажки нет — Texts вернёт
# его дословно. Пустая правка = пустой экран поддержки, а кнопки «сбросить» в
# боте нет, так что откатить это оттуда было бы нечем.


def _undecorated(func):
    """Хендлер обёрнут в @admin_required и @error_handler.

    Разворачиваем до исходной функции: иначе @error_handler проглотит любое
    исключение, и тест останется зелёным на сломанном коде.
    """
    while hasattr(func, '__wrapped__'):
        func = func.__wrapped__
    return func


@pytest.fixture
def crud(monkeypatch):
    """CRUD подменяется там, где его импортировал хендлер."""
    mocks = SimpleNamespace(upsert=AsyncMock(), delete=AsyncMock(), load=AsyncMock())
    monkeypatch.setattr(admin_support, 'upsert_locale_override', mocks.upsert)
    # raising=False: до фикса имя в модуле ещё не импортировано, и без этого
    # RED был бы AttributeError вместо осмысленного «delete не вызван».
    monkeypatch.setattr(admin_support, 'delete_locale_override', mocks.delete, raising=False)
    monkeypatch.setattr(admin_support, 'load_overrides', mocks.load)
    return mocks


async def _submit_description(text: str):
    """Прогнать админский ввод нового описания через хендлер."""
    message = MagicMock()
    message.html_text = text
    message.text = text
    message.answer = AsyncMock()
    db = AsyncMock()
    state = AsyncMock()

    await _undecorated(admin_support.handle_new_desc)(message, _user('ru'), db, state)

    return message, db


async def test_blank_description_resets_to_the_bundled_default(crud):
    """Пустой ввод = сброс к дефолту, а не сохранение пустого override."""
    set_override_cache({('ru', 'SUPPORT_INFO'): 'СТАРЫЙ ТЕКСТ'})

    message, db = await _submit_description('   ')

    crud.delete.assert_awaited_once_with(db, key='SUPPORT_INFO', language='ru')
    crud.upsert.assert_not_awaited()
    db.commit.assert_awaited_once()
    crud.load.assert_awaited_once_with(db)
    message.answer.assert_awaited_once()


async def test_non_blank_description_is_saved_as_an_override(crud):
    message, db = await _submit_description('🛟 Пишите в @support')

    crud.upsert.assert_awaited_once_with(db, key='SUPPORT_INFO', language='ru', value='🛟 Пишите в @support')
    crud.delete.assert_not_awaited()
    db.commit.assert_awaited_once()
    crud.load.assert_awaited_once_with(db)
    message.answer.assert_awaited_once()
