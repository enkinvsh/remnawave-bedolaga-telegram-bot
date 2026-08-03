"""Тесты session request-middleware подстановки кастомных эмодзи."""

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import (
    AnswerCallbackQuery,
    CopyMessage,
    EditMessageCaption,
    EditMessageMedia,
    EditMessageReplyMarkup,
    EditMessageText,
    SendMediaGroup,
    SendMessage,
    SendPaidMedia,
    SendPhoto,
    SendVoice,
    SetMyCommands,
)
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputPaidMediaPhoto,
    KeyboardButton,
    MessageEntity,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.config import settings
from app.middlewares.custom_emoji_request import CustomEmojiRequestMiddleware
from app.utils import custom_emoji as custom_emoji_module


CHECK = '✅'
TOKEN = '123456:AAHnotarealtokenAAHnotarealtokenAAA'


@pytest.fixture(autouse=True)
def _enable_feature(monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOM_EMOJI_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'CUSTOM_EMOJI_TEST_CHAT_IDS', '', raising=False)
    custom_emoji_module.set_enabled_override(None)
    yield
    custom_emoji_module.set_enabled_override(None)


@pytest.fixture
def bot():
    return Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


@pytest.fixture
def calls():
    return []


@pytest.fixture
def make_request(calls):
    async def _make_request(bot, method):
        calls.append(method)
        return method

    return _make_request


@pytest.fixture
def middleware():
    return CustomEmojiRequestMiddleware()


def check_id() -> str:
    return custom_emoji_module.get_mapping().emoji_map[CHECK]


@pytest.mark.asyncio
async def test_default_parse_mode_sentinel_is_resolved(middleware, bot, make_request, calls):
    """ГЛАВНЫЙ ТЕСТ: parse_mode не передан -> Default('parse_mode') -> подстановка ОБЯЗАНА произойти."""
    method = SendMessage(chat_id=1, text=CHECK)

    await middleware(make_request, bot, method)

    assert calls[0].text == f'<tg-emoji emoji-id="{check_id()}">{CHECK}</tg-emoji>'


@pytest.mark.asyncio
async def test_original_method_is_not_mutated(middleware, bot, make_request, calls):
    method = SendMessage(chat_id=1, text=CHECK)

    await middleware(make_request, bot, method)

    assert method.text == CHECK
    assert calls[0] is not method


@pytest.mark.asyncio
async def test_transform_failure_falls_through_to_original(middleware, bot, make_request, calls, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr('app.middlewares.custom_emoji_request.substitute_custom_emoji', boom)
    method = SendMessage(chat_id=1, text=CHECK)

    result = await middleware(make_request, bot, method)

    assert calls == [method]
    assert result is method


@pytest.mark.asyncio
async def test_explicit_html_parse_mode(middleware, bot, make_request, calls):
    method = SendMessage(chat_id=1, text=CHECK, parse_mode='HTML')

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].text


@pytest.mark.asyncio
async def test_markdown_untouched(middleware, bot, make_request, calls):
    method = SendMessage(chat_id=1, text=CHECK, parse_mode='Markdown')

    await middleware(make_request, bot, method)

    assert calls[0].text == CHECK


@pytest.mark.asyncio
async def test_parse_mode_none_untouched(middleware, bot, make_request, calls):
    method = SendMessage(chat_id=1, text=CHECK, parse_mode=None)

    await middleware(make_request, bot, method)

    assert calls[0].text == CHECK


@pytest.mark.asyncio
async def test_entities_present_untouched(middleware, bot, make_request, calls):
    method = SendMessage(chat_id=1, text=f'{CHECK} bold', entities=[MessageEntity(type='bold', offset=0, length=1)])

    await middleware(make_request, bot, method)

    assert calls[0].text == f'{CHECK} bold'


@pytest.mark.asyncio
async def test_caption_entities_present_untouched(middleware, bot, make_request, calls):
    method = SendPhoto(
        chat_id=1,
        photo='file_id',
        caption=CHECK,
        caption_entities=[MessageEntity(type='bold', offset=0, length=1)],
    )

    await middleware(make_request, bot, method)

    assert calls[0].caption == CHECK


@pytest.mark.asyncio
async def test_answer_callback_query_untouched(middleware, bot, make_request, calls):
    method = AnswerCallbackQuery(callback_query_id='1', text=f'{CHECK} ok')

    await middleware(make_request, bot, method)

    assert calls[0].text == f'{CHECK} ok'


@pytest.mark.asyncio
async def test_set_my_commands_untouched(middleware, bot, make_request, calls):
    method = SetMyCommands(commands=[BotCommand(command='start', description=f'{CHECK} старт')])

    await middleware(make_request, bot, method)

    assert calls[0].commands[0].description == f'{CHECK} старт'


@pytest.mark.asyncio
async def test_reply_markup_never_gets_html_tags(middleware, bot, make_request, calls):
    """Кнопки не парсят HTML: в лейбл никогда не попадает <tg-emoji>, только icon_custom_emoji_id."""
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f'{CHECK} кнопка', callback_data='x')]])
    method = SendMessage(chat_id=1, text=CHECK, reply_markup=markup)

    await middleware(make_request, bot, method)

    button = calls[0].reply_markup.inline_keyboard[0][0]
    assert '<tg-emoji' not in button.text
    assert button.text == 'кнопка'
    assert button.icon_custom_emoji_id == check_id()
    assert '<tg-emoji' in calls[0].text


@pytest.mark.asyncio
async def test_edit_message_text_substituted(middleware, bot, make_request, calls):
    method = EditMessageText(chat_id=1, message_id=2, text=CHECK)

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].text


@pytest.mark.asyncio
async def test_edit_message_caption_substituted(middleware, bot, make_request, calls):
    method = EditMessageCaption(chat_id=1, message_id=2, caption=CHECK)

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].caption


@pytest.mark.asyncio
async def test_send_photo_caption_substituted(middleware, bot, make_request, calls):
    method = SendPhoto(chat_id=1, photo='file_id', caption=CHECK)

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].caption


@pytest.mark.asyncio
async def test_media_group_per_item_parse_mode(middleware, bot, make_request, calls):
    method = SendMediaGroup(
        chat_id=1,
        media=[
            InputMediaPhoto(media='a', caption=CHECK),
            InputMediaPhoto(media='b', caption=CHECK, parse_mode='Markdown'),
            InputMediaPhoto(media='c', caption=CHECK, parse_mode='HTML'),
        ],
    )

    await middleware(make_request, bot, method)

    sent = calls[0].media
    assert '<tg-emoji' in sent[0].caption
    assert sent[1].caption == CHECK
    assert '<tg-emoji' in sent[2].caption
    assert method.media[0].caption == CHECK


@pytest.mark.asyncio
async def test_feature_flag_off(middleware, bot, make_request, calls, monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOM_EMOJI_ENABLED', False, raising=False)
    method = SendMessage(chat_id=1, text=CHECK)

    await middleware(make_request, bot, method)

    assert calls[0].text == CHECK


@pytest.mark.asyncio
async def test_canary_chat_included(middleware, bot, make_request, calls, monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOM_EMOJI_TEST_CHAT_IDS', '777, 888', raising=False)
    method = SendMessage(chat_id=888, text=CHECK)

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].text


@pytest.mark.asyncio
async def test_canary_chat_excluded(middleware, bot, make_request, calls, monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOM_EMOJI_TEST_CHAT_IDS', '777,888', raising=False)
    method = SendMessage(chat_id=1, text=CHECK)

    await middleware(make_request, bot, method)

    assert calls[0].text == CHECK


@pytest.mark.asyncio
async def test_idempotent_on_retry(middleware, bot, make_request, calls):
    method = SendMessage(chat_id=1, text=CHECK)

    await middleware(make_request, bot, method)
    await middleware(make_request, bot, calls[0])

    assert calls[1].text == calls[0].text


@pytest.mark.asyncio
async def test_edit_message_media_caption_substituted(middleware, bot, make_request, calls):
    """Прод-баг: caption у EditMessageMedia лежит в method.media.caption (ОДИН объект, не список)."""
    method = EditMessageMedia(
        chat_id=1,
        message_id=2,
        media=InputMediaPhoto(media='x', caption=f'{CHECK} Подписка', parse_mode='HTML'),
    )

    await middleware(make_request, bot, method)

    assert calls[0].media.caption == f'<tg-emoji emoji-id="{check_id()}">{CHECK}</tg-emoji> Подписка'


@pytest.mark.asyncio
async def test_edit_message_media_default_parse_mode(middleware, bot, make_request, calls):
    """У EditMessageMedia НЕТ своего parse_mode: сентинел живёт на элементе media."""
    method = EditMessageMedia(chat_id=1, message_id=2, media=InputMediaPhoto(media='x', caption=CHECK))

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].media.caption


@pytest.mark.asyncio
async def test_edit_message_media_markdown_untouched(middleware, bot, make_request, calls):
    method = EditMessageMedia(
        chat_id=1,
        message_id=2,
        media=InputMediaPhoto(media='x', caption=CHECK, parse_mode='Markdown'),
    )

    await middleware(make_request, bot, method)

    assert calls[0].media.caption == CHECK


@pytest.mark.asyncio
async def test_edit_message_media_caption_entities_untouched(middleware, bot, make_request, calls):
    method = EditMessageMedia(
        chat_id=1,
        message_id=2,
        media=InputMediaPhoto(
            media='x',
            caption=CHECK,
            caption_entities=[MessageEntity(type='bold', offset=0, length=1)],
        ),
    )

    await middleware(make_request, bot, method)

    assert calls[0].media.caption == CHECK


@pytest.mark.asyncio
async def test_edit_message_media_converts_caption_and_buttons(middleware, bot, make_request, calls):
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='💰 Баланс', callback_data='x')]])
    method = EditMessageMedia(
        chat_id=1,
        message_id=2,
        media=InputMediaPhoto(media='x', caption=f'{CHECK} Подписка'),
        reply_markup=markup,
    )

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].media.caption
    assert calls[0].reply_markup.inline_keyboard[0][0].text == 'Баланс'


@pytest.mark.asyncio
async def test_edit_message_media_original_not_mutated(middleware, bot, make_request, calls):
    item = InputMediaPhoto(media='x', caption=CHECK)
    method = EditMessageMedia(chat_id=1, message_id=2, media=item)

    await middleware(make_request, bot, method)

    assert item.caption == CHECK
    assert method.media is item
    assert calls[0] is not method
    assert calls[0].media is not item


@pytest.mark.asyncio
async def test_send_voice_caption_substituted(middleware, bot, make_request, calls):
    method = SendVoice(chat_id=1, voice='file_id', caption=CHECK)

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].caption


@pytest.mark.asyncio
async def test_copy_message_caption_substituted(middleware, bot, make_request, calls):
    method = CopyMessage(chat_id=1, from_chat_id=2, message_id=3, caption=CHECK)

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].caption


@pytest.mark.asyncio
async def test_send_paid_media_caption_substituted_with_explicit_html(middleware, bot, make_request, calls):
    method = SendPaidMedia(
        chat_id=1,
        star_count=1,
        media=[InputPaidMediaPhoto(media='x')],
        caption=CHECK,
        parse_mode='HTML',
    )

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].caption
    assert calls[0].media[0].media == 'x'


@pytest.mark.asyncio
async def test_send_paid_media_without_parse_mode_untouched(middleware, bot, make_request, calls):
    """У SendPaidMedia parse_mode = `str | None = None`, БЕЗ Default-сентинела (аномалия aiogram).

    Значит неявный вызов уходит вообще без parse_mode -> Telegram трактует подпись как
    plain text, и подстановка показала бы юзеру голую разметку.
    """
    method = SendPaidMedia(chat_id=1, star_count=1, media=[InputPaidMediaPhoto(media='x')], caption=CHECK)

    assert method.parse_mode is None

    await middleware(make_request, bot, method)

    assert calls[0].caption == CHECK


MONEY = '💰'
UNMAPPED = '🦄'


def money_id() -> str:
    return custom_emoji_module.get_mapping().emoji_map[MONEY]


def inline(*buttons: InlineKeyboardButton) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(buttons)])


@pytest.mark.asyncio
async def test_inline_button_emoji_becomes_icon(middleware, bot, make_request, calls):
    markup = inline(InlineKeyboardButton(text=f'{MONEY} Баланс', callback_data='menu_balance'))
    method = SendMessage(chat_id=1, text='привет', reply_markup=markup)

    await middleware(make_request, bot, method)

    button = calls[0].reply_markup.inline_keyboard[0][0]
    assert button.text == 'Баланс'
    assert button.icon_custom_emoji_id == money_id()
    assert button.callback_data == 'menu_balance'


@pytest.mark.asyncio
@pytest.mark.parametrize('parse_mode', [None, 'Markdown', 'MarkdownV2'])
async def test_buttons_converted_regardless_of_parse_mode(middleware, bot, make_request, calls, parse_mode):
    """Кнопки не парсят HTML -> HTML-гейт не должен на них влиять."""
    markup = inline(InlineKeyboardButton(text=f'{MONEY} Баланс', callback_data='x'))
    method = SendMessage(chat_id=1, text=f'{CHECK} привет', reply_markup=markup, parse_mode=parse_mode)

    await middleware(make_request, bot, method)

    assert calls[0].reply_markup.inline_keyboard[0][0].text == 'Баланс'
    assert calls[0].reply_markup.inline_keyboard[0][0].icon_custom_emoji_id == money_id()
    assert calls[0].text == f'{CHECK} привет'


@pytest.mark.asyncio
async def test_buttons_converted_when_entities_block_text(middleware, bot, make_request, calls):
    markup = inline(InlineKeyboardButton(text=f'{MONEY} Баланс', callback_data='x'))
    method = SendMessage(
        chat_id=1,
        text=f'{CHECK} привет',
        reply_markup=markup,
        entities=[MessageEntity(type='bold', offset=0, length=1)],
    )

    await middleware(make_request, bot, method)

    assert calls[0].text == f'{CHECK} привет'
    assert calls[0].reply_markup.inline_keyboard[0][0].text == 'Баланс'


@pytest.mark.asyncio
async def test_unmapped_leading_emoji_button_untouched(middleware, bot, make_request, calls):
    markup = inline(InlineKeyboardButton(text=f'{UNMAPPED} Единорог', callback_data='x'))
    method = SendMessage(chat_id=1, text='привет', reply_markup=markup)

    await middleware(make_request, bot, method)

    button = calls[0].reply_markup.inline_keyboard[0][0]
    assert button.text == f'{UNMAPPED} Единорог'
    assert button.icon_custom_emoji_id is None


@pytest.mark.asyncio
async def test_button_with_existing_icon_untouched(middleware, bot, make_request, calls):
    markup = inline(
        InlineKeyboardButton(text=f'{MONEY} Баланс', callback_data='x', icon_custom_emoji_id='5000000000000000009')
    )
    method = SendMessage(chat_id=1, text='привет', reply_markup=markup)

    await middleware(make_request, bot, method)

    button = calls[0].reply_markup.inline_keyboard[0][0]
    assert button.text == f'{MONEY} Баланс'
    assert button.icon_custom_emoji_id == '5000000000000000009'


@pytest.mark.asyncio
async def test_emoji_only_button_untouched(middleware, bot, make_request, calls):
    """Пустой текст кнопки Telegram отвергает -> подстановка запрещена."""
    markup = inline(InlineKeyboardButton(text='⬅\ufe0f', callback_data='back'))
    method = SendMessage(chat_id=1, text='привет', reply_markup=markup)

    await middleware(make_request, bot, method)

    button = calls[0].reply_markup.inline_keyboard[0][0]
    assert button.text == '⬅\ufe0f'
    assert button.icon_custom_emoji_id is None


@pytest.mark.asyncio
async def test_button_without_emoji_untouched(middleware, bot, make_request, calls):
    markup = inline(InlineKeyboardButton(text='Далее', callback_data='next'))
    method = SendMessage(chat_id=1, text='привет', reply_markup=markup)

    await middleware(make_request, bot, method)

    button = calls[0].reply_markup.inline_keyboard[0][0]
    assert button.text == 'Далее'
    assert button.icon_custom_emoji_id is None


@pytest.mark.asyncio
async def test_reply_keyboard_button_converted(middleware, bot, make_request, calls):
    markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=f'{MONEY} Баланс')]], resize_keyboard=True)
    method = SendMessage(chat_id=1, text='привет', reply_markup=markup)

    await middleware(make_request, bot, method)

    button = calls[0].reply_markup.keyboard[0][0]
    assert button.text == 'Баланс'
    assert button.icon_custom_emoji_id == money_id()
    assert calls[0].reply_markup.resize_keyboard is True


@pytest.mark.asyncio
async def test_reply_keyboard_remove_untouched(middleware, bot, make_request, calls):
    method = SendMessage(chat_id=1, text='привет', reply_markup=ReplyKeyboardRemove())

    await middleware(make_request, bot, method)

    assert isinstance(calls[0].reply_markup, ReplyKeyboardRemove)


@pytest.mark.asyncio
async def test_original_markup_not_mutated(middleware, bot, make_request, calls):
    button = InlineKeyboardButton(text=f'{MONEY} Баланс', callback_data='x')
    markup = InlineKeyboardMarkup(inline_keyboard=[[button]])
    method = SendMessage(chat_id=1, text='привет', reply_markup=markup)

    await middleware(make_request, bot, method)

    assert button.text == f'{MONEY} Баланс'
    assert button.icon_custom_emoji_id is None
    assert markup.inline_keyboard[0][0] is button
    assert calls[0].reply_markup is not markup


@pytest.mark.asyncio
async def test_edit_message_reply_markup_converted(middleware, bot, make_request, calls):
    markup = inline(InlineKeyboardButton(text=f'{MONEY} Баланс', callback_data='x'))
    method = EditMessageReplyMarkup(chat_id=1, message_id=2, reply_markup=markup)

    await middleware(make_request, bot, method)

    assert calls[0].reply_markup.inline_keyboard[0][0].text == 'Баланс'


@pytest.mark.asyncio
async def test_multi_row_markup_partially_converted(middleware, bot, make_request, calls):
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f'{MONEY} Баланс', callback_data='a'),
                InlineKeyboardButton(text='Далее', callback_data='b'),
            ],
            [InlineKeyboardButton(text=f'{UNMAPPED} Единорог', callback_data='c')],
            [InlineKeyboardButton(text=f'{CHECK} Готово', callback_data='d')],
        ]
    )
    method = SendMessage(chat_id=1, text='привет', reply_markup=markup)

    await middleware(make_request, bot, method)

    rows = calls[0].reply_markup.inline_keyboard
    assert [len(row) for row in rows] == [2, 1, 1]
    assert [button.callback_data for row in rows for button in row] == ['a', 'b', 'c', 'd']
    assert (rows[0][0].text, rows[0][0].icon_custom_emoji_id) == ('Баланс', money_id())
    assert (rows[0][1].text, rows[0][1].icon_custom_emoji_id) == ('Далее', None)
    assert (rows[1][0].text, rows[1][0].icon_custom_emoji_id) == (f'{UNMAPPED} Единорог', None)
    assert (rows[2][0].text, rows[2][0].icon_custom_emoji_id) == ('Готово', check_id())


@pytest.mark.asyncio
async def test_buttons_untouched_when_flag_off(middleware, bot, make_request, calls, monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOM_EMOJI_ENABLED', False, raising=False)
    markup = inline(InlineKeyboardButton(text=f'{MONEY} Баланс', callback_data='x'))
    method = SendMessage(chat_id=1, text='привет', reply_markup=markup)

    await middleware(make_request, bot, method)

    assert calls[0].reply_markup.inline_keyboard[0][0].text == f'{MONEY} Баланс'


@pytest.mark.asyncio
async def test_buttons_untouched_for_non_canary_chat(middleware, bot, make_request, calls, monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOM_EMOJI_TEST_CHAT_IDS', '777', raising=False)
    markup = inline(InlineKeyboardButton(text=f'{MONEY} Баланс', callback_data='x'))
    method = SendMessage(chat_id=1, text='привет', reply_markup=markup)

    await middleware(make_request, bot, method)

    assert calls[0].reply_markup.inline_keyboard[0][0].text == f'{MONEY} Баланс'


@pytest.mark.asyncio
async def test_buttons_converted_for_canary_chat(middleware, bot, make_request, calls, monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOM_EMOJI_TEST_CHAT_IDS', '777', raising=False)
    markup = inline(InlineKeyboardButton(text=f'{MONEY} Баланс', callback_data='x'))
    method = SendMessage(chat_id=777, text='привет', reply_markup=markup)

    await middleware(make_request, bot, method)

    assert calls[0].reply_markup.inline_keyboard[0][0].text == 'Баланс'


@pytest.mark.asyncio
async def test_button_failure_falls_through_to_original(middleware, bot, make_request, calls, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr('app.middlewares.custom_emoji_request.get_leading_emoji_id', boom)
    markup = inline(InlineKeyboardButton(text=f'{MONEY} Баланс', callback_data='x'))
    method = SendMessage(chat_id=1, text='привет', reply_markup=markup)

    result = await middleware(make_request, bot, method)

    assert calls == [method]
    assert result is method


@pytest.mark.asyncio
async def test_db_override_can_enable_when_env_flag_is_off(middleware, bot, make_request, calls, monkeypatch):
    """Настройка из БД (кеш в памяти) главнее env — иначе админка не работала бы без редеплоя."""
    monkeypatch.setattr(settings, 'CUSTOM_EMOJI_ENABLED', False, raising=False)
    custom_emoji_module.set_enabled_override(True)
    method = SendMessage(chat_id=1, text=CHECK)

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].text


@pytest.mark.asyncio
async def test_db_override_can_disable_when_env_flag_is_on(middleware, bot, make_request, calls, monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOM_EMOJI_ENABLED', True, raising=False)
    custom_emoji_module.set_enabled_override(False)
    method = SendMessage(chat_id=1, text=CHECK)

    await middleware(make_request, bot, method)

    assert calls[0].text == CHECK


@pytest.mark.asyncio
async def test_env_flag_used_when_no_db_override(middleware, bot, make_request, calls, monkeypatch):
    monkeypatch.setattr(settings, 'CUSTOM_EMOJI_ENABLED', False, raising=False)
    custom_emoji_module.set_enabled_override(None)
    method = SendMessage(chat_id=1, text=CHECK)

    await middleware(make_request, bot, method)

    assert calls[0].text == CHECK


@pytest.mark.asyncio
async def test_middleware_never_touches_the_database(middleware, bot, make_request, calls, monkeypatch):
    """Middleware на горячем пути: запрос в БД на каждое сообщение недопустим."""
    import app.database.database as database_module

    def boom(*args, **kwargs):
        raise AssertionError('middleware must not open a DB session')

    monkeypatch.setattr(database_module, 'AsyncSessionLocal', boom)
    method = SendMessage(chat_id=1, text=CHECK)

    await middleware(make_request, bot, method)

    assert '<tg-emoji' in calls[0].text


@pytest.mark.asyncio
async def test_runtime_mapping_replacement_is_used(middleware, bot, make_request, calls):
    """set_mapping из админки должен действовать НЕМЕДЛЕННО, без рестарта."""
    custom_emoji_module.set_mapping(custom_emoji_module.build_mapping({CHECK: '424242'}))
    try:
        method = SendMessage(chat_id=1, text=CHECK)

        await middleware(make_request, bot, method)

        assert calls[0].text == f'<tg-emoji emoji-id="424242">{CHECK}</tg-emoji>'
    finally:
        custom_emoji_module.reset_mapping_cache()


def test_create_bot_registers_middleware():
    from app.bot_factory import create_bot

    created = create_bot(token=TOKEN)

    registered = [type(item) for item in created.session.middleware._middlewares]
    assert CustomEmojiRequestMiddleware in registered
