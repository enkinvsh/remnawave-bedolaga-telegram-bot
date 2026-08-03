"""Админ-раздел: паки кастомных эмодзи, настраиваемые без редеплоя."""

import structlog
from aiogram import Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.database.database import AsyncSessionLocal
from app.localization.texts import get_texts
from app.services.custom_emoji_pack_service import (
    PackLinkError,
    PackNotFoundError,
    PackTypeError,
    apply_bundled_mapping,
    compute_coverage,
    get_pack_names,
    is_enabled,
    parse_pack_link,
    rebuild_and_apply,
    save_pack_names,
    set_enabled,
)
from app.states import CustomEmojiStates
from app.utils.custom_emoji import get_mapping
from app.utils.decorators import admin_required


logger = structlog.get_logger(__name__)

router = Router(name='admin_custom_emoji')

MAX_PACKS = 10


def _coverage_line(coverage: dict[str, int]) -> str:
    return (
        f'{coverage["percent"]}% ({coverage["occ_hit"]} / {coverage["occ_total"]} упоминаний, '
        f'{coverage["unique_hit"]} / {coverage["unique_total"]} эмодзи)'
    )


def _root_text(enabled: bool, packs: list[str], count: int, coverage: dict[str, int]) -> str:
    state = '🟢 Включено' if enabled else '🔴 Выключено'
    if packs:
        packs_line = '\n'.join(f'{index}. <code>{name}</code>' for index, name in enumerate(packs, start=1))
        source_line = 'Источник: паки ниже (в порядке приоритета, первый выигрывает).'
    else:
        packs_line = '<i>не заданы</i>'
        source_line = 'Источник: встроенная карта из образа.'

    return (
        '😀 <b>Кастомные эмодзи</b>\n\n'
        f'<b>Состояние:</b> {state}\n'
        f'<b>Иконок в карте:</b> {count}\n'
        f'<b>Покрытие:</b> {_coverage_line(coverage)}\n\n'
        f'{source_line}\n'
        f'<b>Паки:</b>\n{packs_line}'
    )


def _root_keyboard(enabled: bool, packs: list[str], language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    toggle_text = texts.t('ADMIN_CUSTOM_EMOJI_DISABLE', '🔴 Выключить')
    if not enabled:
        toggle_text = texts.t('ADMIN_CUSTOM_EMOJI_ENABLE', '🟢 Включить')

    rows = [
        [InlineKeyboardButton(text=toggle_text, callback_data='admin_ce:toggle')],
        [InlineKeyboardButton(text=texts.t('ADMIN_CUSTOM_EMOJI_ADD', '➕ Добавить пак'), callback_data='admin_ce:add')],
    ]
    if packs:
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_CUSTOM_EMOJI_REMOVE', '🗑 Удалить пак'), callback_data='admin_ce:remove'
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=texts.t('ADMIN_CUSTOM_EMOJI_RESET', '🔄 Сбросить на встроенный'),
                    callback_data='admin_ce:reset',
                )
            ]
        )
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_submenu_settings')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cancel_keyboard(language: str) -> InlineKeyboardMarkup:
    texts = get_texts(language)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.t('ADMIN_CUSTOM_EMOJI_CANCEL', '◀️ Отмена'), callback_data='admin_ce:root')]
        ]
    )


def _language(db_user) -> str:
    return getattr(db_user, 'language', None) or 'ru'


async def _render_root(message: Message, language: str) -> None:
    async with AsyncSessionLocal() as db:
        enabled = await is_enabled(db)
        packs = await get_pack_names(db)

    mapping = dict(get_mapping().emoji_map)
    await message.edit_text(
        _root_text(enabled, packs, len(mapping), compute_coverage(mapping)),
        reply_markup=_root_keyboard(enabled, packs, language),
    )


@router.callback_query(F.data.in_({'admin_custom_emoji', 'admin_ce:root'}))
@admin_required
async def show_custom_emoji_panel(callback: CallbackQuery, state: FSMContext, db_user=None, **kwargs) -> None:
    await state.clear()
    await _render_root(callback.message, _language(db_user))
    await callback.answer()


@router.callback_query(F.data == 'admin_ce:toggle')
@admin_required
async def toggle_custom_emoji(callback: CallbackQuery, state: FSMContext, db_user=None, **kwargs) -> None:
    await state.clear()
    async with AsyncSessionLocal() as db:
        new_value = not await is_enabled(db)
        await set_enabled(db, new_value)
        await db.commit()

    await callback.answer('Включено' if new_value else 'Выключено')
    await _render_root(callback.message, _language(db_user))


@router.callback_query(F.data == 'admin_ce:add')
@admin_required
async def prompt_pack_link(callback: CallbackQuery, state: FSMContext, db_user=None, **kwargs) -> None:
    await state.set_state(CustomEmojiStates.waiting_for_pack_link)
    await callback.message.edit_text(
        '➕ <b>Добавить пак кастомных эмодзи</b>\n\n'
        'Отправьте ссылку на пак, например:\n'
        '<code>https://t.me/addemoji/TgAndroidIcons</code>\n\n'
        'Новый пак встаёт в конец очереди приоритета: иконки уже добавленных паков не перебиваются.',
        reply_markup=_cancel_keyboard(_language(db_user)),
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_ce:remove')
@admin_required
async def show_remove_list(callback: CallbackQuery, state: FSMContext, db_user=None, **kwargs) -> None:
    await state.clear()
    language = _language(db_user)
    texts = get_texts(language)

    async with AsyncSessionLocal() as db:
        packs = await get_pack_names(db)

    if not packs:
        await callback.answer('Паки не заданы', show_alert=True)
        return

    rows = [[InlineKeyboardButton(text=f'🗑 {name}', callback_data=f'admin_ce:rm:{name}')] for name in packs[:MAX_PACKS]]
    rows.append([InlineKeyboardButton(text=texts.BACK, callback_data='admin_ce:root')])

    await callback.message.edit_text(
        '🗑 <b>Удаление пака</b>\n\nВыберите пак, который нужно убрать из карты.',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('admin_ce:rm:'))
@admin_required
async def remove_pack(callback: CallbackQuery, state: FSMContext, db_user=None, **kwargs) -> None:
    await state.clear()
    name = callback.data.split(':', 2)[2]

    async with AsyncSessionLocal() as db:
        packs = [item for item in await get_pack_names(db) if item != name]
        await save_pack_names(db, packs)
        await db.commit()

    try:
        await rebuild_and_apply(callback.bot, packs)
    except Exception as error:
        logger.warning('Не удалось пересобрать карту после удаления пака', pack=name, error=str(error))
        await callback.answer('Пак убран, но карту пересобрать не удалось', show_alert=True)
    else:
        await callback.answer(f'Пак {name} удалён')

    await _render_root(callback.message, _language(db_user))


@router.callback_query(F.data == 'admin_ce:reset')
@admin_required
async def reset_to_bundled(callback: CallbackQuery, state: FSMContext, db_user=None, **kwargs) -> None:
    await state.clear()
    async with AsyncSessionLocal() as db:
        await save_pack_names(db, [])
        await db.commit()

    apply_bundled_mapping()
    await callback.answer('Возврат на встроенную карту')
    await _render_root(callback.message, _language(db_user))


@router.message(CustomEmojiStates.waiting_for_pack_link)
@admin_required
async def process_pack_link(message: Message, state: FSMContext, db_user=None, **kwargs) -> None:
    language = _language(db_user)

    name = parse_pack_link(message.text or '')
    if name is None:
        await message.answer(
            '❌ Не удалось разобрать ссылку. Пришлите ссылку вида '
            '<code>https://t.me/addemoji/ИмяПака</code> или само имя пака.',
            reply_markup=_cancel_keyboard(language),
        )
        return

    async with AsyncSessionLocal() as db:
        packs = await get_pack_names(db)

    if name in packs:
        await message.answer(f'ℹ️ Пак <code>{name}</code> уже подключён.', reply_markup=_cancel_keyboard(language))
        return
    if len(packs) >= MAX_PACKS:
        await message.answer(f'❌ Больше {MAX_PACKS} паков не поддерживается.', reply_markup=_cancel_keyboard(language))
        return

    before = compute_coverage(dict(get_mapping().emoji_map))
    candidate = [*packs, name]

    try:
        summary = await rebuild_and_apply(message.bot, candidate)
    except PackLinkError:
        await message.answer('❌ Некорректное имя пака.', reply_markup=_cancel_keyboard(language))
        return
    except PackTypeError:
        await message.answer(
            '❌ Это обычный стикерпак, а не пак кастомных эмодзи.', reply_markup=_cancel_keyboard(language)
        )
        return
    except PackNotFoundError:
        await message.answer(
            f'❌ Пак <code>{name}</code> не найден. Проверьте ссылку и доступность пака.',
            reply_markup=_cancel_keyboard(language),
        )
        return
    except Exception as error:
        logger.warning('Ошибка добавления пака кастомных эмодзи', pack=name, error=str(error))
        await message.answer('❌ Не удалось подключить пак. Попробуйте позже.', reply_markup=_cancel_keyboard(language))
        return

    async with AsyncSessionLocal() as db:
        await save_pack_names(db, candidate)
        enabled = await is_enabled(db)
        await db.commit()

    await state.clear()
    after = summary['coverage']
    await message.answer(
        f'✅ Пак <code>{name}</code>: {summary["count"]} иконок. '
        f'Покрытие выросло {before["percent"]}% → {after["percent"]}%',
        reply_markup=_root_keyboard(enabled, candidate, language),
    )


def register_handlers(dp: Dispatcher) -> None:
    dp.include_router(router)
