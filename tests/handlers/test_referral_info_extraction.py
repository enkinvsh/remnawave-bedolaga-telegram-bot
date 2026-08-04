"""Экран «Партнерка» (кнопка menu_referrals) — сборка текста.

``show_referral_info`` принимает ``CallbackQuery`` и берёт username бота из
``callback.bot.get_me()``, поэтому превью в кабинете его не вызовет. Сборка
текста вынесена в ``build_referral_info_text(user, texts, db, bot_username)``;
хендлер зовёт её же — копия форматирования разъехалась бы с ботом.

Тесты фиксируют две вещи:

1. **Характеризация** — текст, который хендлер отправляет в Telegram, собран из
   тех же блоков, что и сегодня. Проверки зелёные и ДО, и ПОСЛЕ выноса.
2. **Проводка** — билдер существует и отдаёт ровно то, что уходит в
   ``edit_or_answer_photo``.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
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


def _summary(**overrides) -> dict:
    summary = {
        'invited_count': 7,
        'paid_referrals_count': 3,
        'active_referrals_count': 2,
        'total_earned_kopeks': 150_000,
        'month_earned_kopeks': 40_000,
        'recent_earnings': [],
        'earnings_by_type': {},
        'conversion_rate': 42.9,
    }
    summary.update(overrides)
    return summary


def _user(**overrides) -> SimpleNamespace:
    # SimpleNamespace, а не MagicMock: get_effective_referral_commission_percent
    # сравнивает referral_commission_percent с числом, и MagicMock уронил бы
    # сравнение TypeError'ом.
    user = SimpleNamespace(
        id=1,
        telegram_id=468130024,
        language='ru',
        referral_code='DEMO0000',
        referral_commission_percent=None,
    )
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


@pytest.fixture
def isolate(monkeypatch):
    """Отрезаем клавиатуру, отправку и запрос статистики — на текст они не влияют."""
    from app.handlers import referral

    monkeypatch.setattr(referral, 'get_referral_keyboard', MagicMock(return_value=None))
    monkeypatch.setattr(referral, 'edit_or_answer_photo', AsyncMock())
    return referral


async def _run_handler(referral_module, user, summary, db=None) -> str:
    """Прогоняет реальный хендлер и возвращает текст, ушедший в Telegram."""
    from app.handlers.referral import show_referral_info

    callback = MagicMock()
    callback.answer = AsyncMock()
    callback.bot = MagicMock()
    callback.bot.get_me = AsyncMock(return_value=SimpleNamespace(username='dropwebpay_bot'))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr('app.handlers.referral.get_user_referral_summary', AsyncMock(return_value=summary))
        await show_referral_info(callback, user, db or MagicMock())

    referral_module.edit_or_answer_photo.assert_awaited_once()
    return referral_module.edit_or_answer_photo.await_args.args[1]


# ============ 1. Характеризация ============


async def test_header_and_stats_block(isolate, texts):
    result = await _run_handler(isolate, _user(), _summary())

    assert result.startswith(
        texts.t('REFERRAL_PROGRAM_TITLE', '')
        + '\n\n'
        + texts.t('REFERRAL_STATS_HEADER', '')
        + '\n'
        + texts.t('REFERRAL_STATS_INVITED', '').format(count=7)
        + '\n'
        + texts.t('REFERRAL_STATS_FIRST_TOPUPS', '').format(count=3)
        + '\n'
        + texts.t('REFERRAL_STATS_ACTIVE', '').format(count=2)
        + '\n'
        + texts.t('REFERRAL_STATS_CONVERSION', '').format(rate=42.9)
        + '\n'
        + texts.t('REFERRAL_STATS_TOTAL_EARNED', '').format(amount=texts.format_price(150_000))
        + '\n'
        + texts.t('REFERRAL_STATS_MONTH_EARNED', '').format(amount=texts.format_price(40_000))
        + '\n\n'
        + texts.t('REFERRAL_REWARDS_HEADER', '')
    )


async def test_new_user_and_inviter_bonus_lines(isolate, monkeypatch, texts):
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 10_000)
    monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 20_000)
    monkeypatch.setattr(settings, 'REFERRAL_MINIMUM_TOPUP_KOPEKS', 30_000)

    result = await _run_handler(isolate, _user(), _summary())

    assert (
        texts.t('REFERRAL_REWARD_NEW_USER', '').format(
            bonus=texts.format_price(10_000),
            minimum=texts.format_price(30_000),
        )
        in result
    )
    assert texts.t('REFERRAL_REWARD_INVITER', '').format(bonus=texts.format_price(20_000)) in result


async def test_bonus_lines_disappear_at_zero(isolate, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_FIRST_TOPUP_BONUS_KOPEKS', 0)
    monkeypatch.setattr(settings, 'REFERRAL_INVITER_BONUS_KOPEKS', 0)

    result = await _run_handler(isolate, _user(), _summary())

    assert 'Новый пользователь получает' not in result
    assert 'Вы получаете при первом пополнении' not in result


async def test_unlimited_commission_line(isolate, monkeypatch, texts):
    monkeypatch.setattr(settings, 'REFERRAL_MAX_COMMISSION_PAYMENTS', 0)
    monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 25)

    result = await _run_handler(isolate, _user(), _summary())

    assert texts.t('REFERRAL_REWARD_COMMISSION', '').format(percent=25) in result
    assert 'с первых' not in result


async def test_limited_commission_line(isolate, monkeypatch, texts):
    monkeypatch.setattr(settings, 'REFERRAL_MAX_COMMISSION_PAYMENTS', 5)
    monkeypatch.setattr(settings, 'REFERRAL_COMMISSION_PERCENT', 30)

    result = await _run_handler(isolate, _user(), _summary())

    assert texts.t('REFERRAL_REWARD_COMMISSION_LIMITED', '').format(percent=30, max_payments=5) in result


async def test_bot_link_is_plain_not_wrapped_in_code(isolate, texts):
    """Ссылка на бота отдаётся без <code> — так её видит пользователь сегодня."""
    result = await _run_handler(isolate, _user(), _summary())

    link = settings.get_bot_referral_link('DEMO0000', 'dropwebpay_bot')
    assert texts.t('REFERRAL_BOT_LINK_TITLE', '') + f'\n{link}\n' in result
    assert f'<code>{link}</code>' not in result


async def test_referral_code_line(isolate, texts):
    result = await _run_handler(isolate, _user(), _summary())

    assert texts.t('REFERRAL_CODE_TITLE', '').format(code='DEMO0000') in result


async def test_recent_earnings_block(isolate, texts):
    summary = _summary(
        recent_earnings=[
            {'amount_kopeks': 5_000, 'reason': 'referral_first_topup', 'referral_name': 'Пётр'},
            {'amount_kopeks': 0, 'reason': 'referral_commission', 'referral_name': 'Нулевой'},
        ]
    )

    result = await _run_handler(isolate, _user(), summary)

    assert texts.t('REFERRAL_RECENT_EARNINGS_HEADER', '') in result
    assert (
        texts.t('REFERRAL_RECENT_EARNINGS_ITEM', '').format(
            reason=texts.t('REFERRAL_EARNING_REASON_FIRST_TOPUP', ''),
            amount=texts.format_price(5_000),
            referral_name='Пётр',
        )
        in result
    )
    # Нулевые начисления отсеиваются — это поведение бота, не баг.
    assert 'Нулевой' not in result


async def test_earnings_by_type_block(isolate, texts):
    summary = _summary(
        earnings_by_type={
            'referral_first_topup': {'count': 2, 'total_amount_kopeks': 20_000},
            'referral_commission_topup': {'count': 3, 'total_amount_kopeks': 30_000},
            'referral_commission': {'count': 4, 'total_amount_kopeks': 40_000},
        }
    )

    result = await _run_handler(isolate, _user(), summary)

    assert texts.t('REFERRAL_EARNINGS_BY_TYPE_HEADER', '') in result
    assert texts.t('REFERRAL_EARNINGS_FIRST_TOPUPS', '').format(count=2, amount=texts.format_price(20_000)) in result
    assert texts.t('REFERRAL_EARNINGS_TOPUPS', '').format(count=3, amount=texts.format_price(30_000)) in result
    assert texts.t('REFERRAL_EARNINGS_PURCHASES', '').format(count=4, amount=texts.format_price(40_000)) in result


async def test_zero_amount_type_rows_are_skipped(isolate):
    summary = _summary(earnings_by_type={'referral_first_topup': {'count': 2, 'total_amount_kopeks': 0}})

    result = await _run_handler(isolate, _user(), summary)

    assert 'Бонусы за первые пополнения' not in result


async def test_footer_closes_the_screen(isolate, texts):
    result = await _run_handler(isolate, _user(), _summary())

    assert result.endswith(texts.t('REFERRAL_INVITE_FOOTER', ''))


async def test_html_escapes_the_referral_name(isolate):
    summary = _summary(
        recent_earnings=[{'amount_kopeks': 5_000, 'reason': 'referral_commission', 'referral_name': '<b>злой</b>'}]
    )

    result = await _run_handler(isolate, _user(), summary)

    assert '&lt;b&gt;злой&lt;/b&gt;' in result


# ============ Пути-алерты: это не экран ============


async def test_disabled_program_answers_with_an_alert(isolate, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', False)
    from app.handlers.referral import show_referral_info

    callback = MagicMock()
    callback.answer = AsyncMock()

    await show_referral_info(callback, _user(), MagicMock())

    isolate.edit_or_answer_photo.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True


async def test_missing_referral_code_answers_with_an_alert(isolate, monkeypatch):
    monkeypatch.setattr(settings, 'REFERRAL_PROGRAM_ENABLED', True)
    from app.handlers.referral import show_referral_info

    callback = MagicMock()
    callback.answer = AsyncMock()

    await show_referral_info(callback, _user(referral_code=None), MagicMock())

    isolate.edit_or_answer_photo.assert_not_awaited()
    assert callback.answer.await_args.kwargs['show_alert'] is True


# ============ 2. Проводка: билдер общий с хендлером ============


async def test_builder_returns_exactly_what_the_handler_sends(isolate, texts, monkeypatch):
    from app.handlers.referral import build_referral_info_text

    user = _user()
    summary = _summary(
        recent_earnings=[{'amount_kopeks': 5_000, 'reason': 'referral_first_topup', 'referral_name': 'Пётр'}],
        earnings_by_type={'referral_commission': {'count': 4, 'total_amount_kopeks': 40_000}},
    )

    handler_text = await _run_handler(isolate, user, summary)

    monkeypatch.setattr('app.handlers.referral.get_user_referral_summary', AsyncMock(return_value=summary))
    builder_text = await build_referral_info_text(user, texts, MagicMock(), 'dropwebpay_bot')

    assert builder_text == handler_text


async def test_builder_falls_back_to_the_configured_bot_username(texts, monkeypatch):
    """Превью зовёт билдер без username — ссылка обязана остаться валидной."""
    from app.handlers.referral import build_referral_info_text

    monkeypatch.setattr('app.handlers.referral.get_user_referral_summary', AsyncMock(return_value=_summary()))

    result = await build_referral_info_text(_user(), texts, MagicMock())

    assert settings.get_bot_referral_link('DEMO0000') in result


async def test_builder_keys_are_overridable(texts, monkeypatch):
    from app.handlers.referral import build_referral_info_text

    monkeypatch.setattr('app.handlers.referral.get_user_referral_summary', AsyncMock(return_value=_summary()))
    set_override_cache({('ru', 'REFERRAL_PROGRAM_TITLE'): '!!ЗАМЕНА!!'})

    result = await build_referral_info_text(_user(), texts, MagicMock(), 'dropwebpay_bot')

    assert result.startswith('!!ЗАМЕНА!!')
