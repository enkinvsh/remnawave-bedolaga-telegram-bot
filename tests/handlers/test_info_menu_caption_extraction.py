"""Экран «Инфо» (кнопка menu_info) — сборка подписи.

``show_info_menu`` принимает ``CallbackQuery``, поэтому превью в кабинете его не
вызовет. Подпись собирается из двух ключей с условием («пустой prompt — значит
только заголовок»), и копировать это условие в рендерер нельзя: копия
разъехалась бы с ботом. Сборка живёт в ``build_info_menu_caption(texts)``,
хендлер зовёт её же.

Тесты фиксируют две вещи:

1. **Характеризация** — подпись, которую хендлер отправляет в Telegram,
   байт-в-байт совпадает с сегодняшней. Проверки зелёные и ДО, и ПОСЛЕ выноса.
2. **Проводка** — билдер существует и отдаёт ровно то, что уходит в
   ``edit_or_answer_photo``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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


@pytest.fixture
def isolate(monkeypatch):
    """Отрезаем клавиатуру, отправку и запросы к БД — на подпись они не влияют."""
    from app.handlers import menu

    monkeypatch.setattr(menu, 'edit_or_answer_photo', AsyncMock())
    monkeypatch.setattr(menu, 'get_info_menu_keyboard', MagicMock(return_value=None))
    monkeypatch.setattr(menu, 'has_auto_assign_promo_groups', AsyncMock(return_value=False))
    monkeypatch.setattr(menu, 'get_all_info_pages', AsyncMock(return_value=[]))
    monkeypatch.setattr(menu.PrivacyPolicyService, 'is_policy_enabled', AsyncMock(return_value=False))
    monkeypatch.setattr(menu.PublicOfferService, 'is_offer_enabled', AsyncMock(return_value=False))
    monkeypatch.setattr(menu.FaqService, 'is_enabled', AsyncMock(return_value=False))
    return menu


async def _run_handler(menu_module) -> str:
    """Прогоняет реальный хендлер и возвращает подпись, ушедшую в Telegram."""
    from app.handlers.menu import show_info_menu

    callback = MagicMock()
    callback.answer = AsyncMock()

    await show_info_menu(callback, SimpleNamespace(id=1, language='ru'), MagicMock())

    menu_module.edit_or_answer_photo.assert_awaited_once()
    return menu_module.edit_or_answer_photo.await_args.kwargs['caption']


# ============ 1. Характеризация ============


async def test_caption_is_header_then_blank_line_then_prompt(isolate, texts):
    result = await _run_handler(isolate)

    assert result == texts.t('MENU_INFO_HEADER', '') + '\n\n' + texts.t('MENU_INFO_PROMPT', '')


async def test_caption_uses_the_bundled_strings(isolate):
    result = await _run_handler(isolate)

    assert result == 'ℹ️ <b>Инфо</b>\n\nВыберите раздел:'


async def test_empty_prompt_leaves_only_the_header(isolate, texts):
    """Владелец стирает подсказку — хвостовые переводы строк не должны остаться."""
    set_override_cache({('ru', 'MENU_INFO_PROMPT'): ''})

    result = await _run_handler(isolate)

    assert result == texts.t('MENU_INFO_HEADER', '')
    assert not result.endswith('\n')


async def test_both_keys_are_overridable(isolate):
    set_override_cache(
        {
            ('ru', 'MENU_INFO_HEADER'): 'ЗАГОЛОВОК',
            ('ru', 'MENU_INFO_PROMPT'): 'ПОДСКАЗКА',
        }
    )

    assert await _run_handler(isolate) == 'ЗАГОЛОВОК\n\nПОДСКАЗКА'


# ============ 2. Проводка: билдер общий с хендлером ============


async def test_builder_returns_exactly_what_the_handler_sends(isolate, texts):
    from app.handlers.menu import build_info_menu_caption

    handler_caption = await _run_handler(isolate)

    assert build_info_menu_caption(texts) == handler_caption


def test_builder_drops_the_separator_for_an_empty_prompt(texts):
    from app.handlers.menu import build_info_menu_caption

    set_override_cache({('ru', 'MENU_INFO_PROMPT'): ''})

    assert build_info_menu_caption(texts) == texts.t('MENU_INFO_HEADER', '')
