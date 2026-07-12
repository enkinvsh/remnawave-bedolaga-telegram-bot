"""SendRichMessage custom aiogram method — serialization + one-call dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.services.telegram_rich import SendRichMessage


def test_api_method_and_return_type():
    assert SendRichMessage.__api_method__ == 'sendRichMessage'
    assert SendRichMessage.__returning__ is Message


def test_payload_dump_shape():
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Open', url='https://a.io')]])
    method = SendRichMessage(
        chat_id=-1001,
        message_thread_id=7,
        rich_message={'html': '<b>hi</b>', 'skip_entity_detection': True},
        reply_markup=kb,
    )
    dump = method.model_dump(exclude_none=True)
    assert dump['chat_id'] == -1001
    assert dump['message_thread_id'] == 7
    assert dump['rich_message'] == {'html': '<b>hi</b>', 'skip_entity_detection': True}
    assert isinstance(dump['reply_markup'], dict)
    assert dump['reply_markup']['inline_keyboard'][0][0]['url'] == 'https://a.io'


def test_thread_and_markup_omitted_when_none():
    method = SendRichMessage(chat_id=-1001, rich_message={'html': 'x', 'skip_entity_detection': True})
    dump = method.model_dump(exclude_none=True)
    assert 'message_thread_id' not in dump
    assert 'reply_markup' not in dump


@pytest.mark.asyncio
async def test_single_bot_call_with_method():
    bot = AsyncMock(return_value=Message.model_construct(message_id=999))
    method = SendRichMessage(chat_id=-1001, rich_message={'html': 'x', 'skip_entity_detection': True})
    result = await bot(method)
    assert bot.await_count == 1
    passed = bot.await_args.args[0]
    assert isinstance(passed, SendRichMessage)
    assert passed.__api_method__ == 'sendRichMessage'
    assert result.message_id == 999
