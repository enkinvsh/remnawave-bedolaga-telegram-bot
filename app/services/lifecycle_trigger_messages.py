"""Lifecycle-триггеры: слой сообщений (задача C2).

Кандидат-DTO, рендер шаблонов, сборка клавиатур из ключей кнопок (реюз существующих
callback'ов бота), доставка через хелпер monitoring_service и по-правилу обработчики.
Выборку аудитории и оркестрацию делает ``lifecycle_trigger_service`` — он импортирует
обработчики отсюда (граф импортов односторонний, без циклов).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.discount_offer import upsert_discount_offer
from app.database.crud.notification import notification_sent, record_notification
from app.database.models import Subscription, User
from app.localization.texts import get_texts
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.utils.timezone import format_local_datetime


logger = structlog.get_logger(__name__)

_LEGACY_TRIAL_END_TYPE = 'trial_2h'
_LADDER_OFFER_TYPE = 'post_trial_ladder'


@dataclass(frozen=True, slots=True)
class LifecycleCandidate:
    """Кандидат на отправку: пользователь + номер шага (occurrence) + контекст рендера."""

    user: User
    occurrence: int
    subscription: Subscription | None = None
    offset_hours: int | None = None
    step: dict[str, Any] = field(default_factory=dict)


def render_template(
    template: str,
    *,
    days: Any = None,
    percent: Any = None,
    date: Any = None,
    hours: Any = None,
) -> str:
    """Подставляет плейсхолдеры ``{days}/{percent}/{date}/{hours}`` (только переданные)."""
    result = template
    for token, value in (('{days}', days), ('{percent}', percent), ('{date}', date), ('{hours}', hours)):
        if value is not None:
            result = result.replace(token, str(value))
    return result


def _build_button(key: str, texts: Any) -> InlineKeyboardButton | None:
    if key == 'activate_trial':
        return build_miniapp_or_callback_button(
            text=texts.t('TRIAL_ACTIVATE_BUTTON', '🎁 Активировать'), callback_data='trial_activate'
        )
    if key == 'connect':
        return build_miniapp_or_callback_button(
            text=texts.t('CONNECT_BUTTON', '🔗 Подключиться'), callback_data='subscription_connect'
        )
    if key == 'my_subscription':
        return build_miniapp_or_callback_button(
            text=texts.t('BTN_MY_SUBSCRIPTION', '📱 Моя подписка'), callback_data='menu_subscription'
        )
    if key == 'buy':
        return build_miniapp_or_callback_button(
            text=texts.t('SUBSCRIPTION_BUY', '💎 Купить подписку'), callback_data='menu_buy'
        )
    if key == 'balance_topup':
        return build_miniapp_or_callback_button(
            text=texts.t('BALANCE_TOPUP', '💳 Пополнить баланс'), callback_data='balance_topup'
        )
    if key == 'support':
        return InlineKeyboardButton(text=texts.t('SUPPORT_BUTTON', '🆘 Поддержка'), callback_data='menu_support')
    return None


def _keyboard_from_keys(keys: list[str], texts: Any) -> InlineKeyboardMarkup | None:
    rows = [[button] for button in (_build_button(key, texts) for key in keys) if button is not None]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _is_unreachable(error: TelegramBadRequest) -> bool:
    message = str(error).lower()
    markers = (
        'chat not found',
        'user is deactivated',
        'bot was blocked by the user',
        "bot can't initiate conversation",
        'user not found',
        'peer id invalid',
    )
    return any(marker in message for marker in markers)


async def deliver(send_message: Any, *, user: User, text: str, keyboard: InlineKeyboardMarkup | None) -> bool:
    """Отправка через хелпер monitoring_service. True = доставлено ИЛИ адресат недоступен
    (не повторяем); False = временная ошибка (повторим на следующем прогоне)."""
    try:
        await send_message(chat_id=user.telegram_id, text=text, reply_markup=keyboard, user=user)
        return True
    except TelegramForbiddenError:
        logger.warning('lifecycle: адресат заблокировал бота', user_id=user.id)
        return True
    except TelegramBadRequest as exc:
        if _is_unreachable(exc):
            logger.warning('lifecycle: адресат недоступен', user_id=user.id)
            return True
        logger.warning('lifecycle: некорректный запрос при отправке', user_id=user.id, exc=exc)
        return False
    except TelegramNetworkError as exc:
        logger.warning('lifecycle: сетевая ошибка при отправке', user_id=user.id, exc=exc)
        return False


async def handle_trial_not_activated(
    db: AsyncSession, send_message: Any, candidate: LifecycleCandidate, config: dict[str, Any], now: datetime
) -> bool:
    texts = get_texts(candidate.user.language)
    template = (
        config.get('message_template', '')
        if candidate.occurrence == 1
        else config.get('repeat_message_template') or config.get('message_template', '')
    )
    keyboard = _keyboard_from_keys([config.get('button', 'activate_trial')], texts)
    return await deliver(send_message, user=candidate.user, text=render_template(template), keyboard=keyboard)


async def handle_trial_zero_traffic(
    db: AsyncSession, send_message: Any, candidate: LifecycleCandidate, config: dict[str, Any], now: datetime
) -> bool:
    texts = get_texts(candidate.user.language)
    templates = config.get('message_templates', {})
    template = templates.get(str(candidate.offset_hours)) or next(iter(templates.values()), '')
    keyboard = _keyboard_from_keys(config.get('buttons', []), texts)
    return await deliver(send_message, user=candidate.user, text=render_template(template), keyboard=keyboard)


async def handle_trial_ending(
    db: AsyncSession, send_message: Any, candidate: LifecycleCandidate, config: dict[str, Any], now: datetime
) -> bool:
    """Коордиинируется с легаси ``_check_trial_expiring_soon`` (``trial_2h``) через общий
    журнал ``notification_sent``/``record_notification`` — двойного 2ч-предупреждения не будет."""
    subscription = candidate.subscription
    if subscription is not None and await notification_sent(
        db, candidate.user.id, subscription.id, _LEGACY_TRIAL_END_TYPE
    ):
        return True

    texts = get_texts(candidate.user.language)
    hours = int(config.get('hours_before', 2))
    message = render_template(config.get('message_template', ''), hours=hours)
    keyboard = _keyboard_from_keys(config.get('buttons') or ['buy', 'balance_topup'], texts)
    delivered = await deliver(send_message, user=candidate.user, text=message, keyboard=keyboard)
    if delivered and subscription is not None:
        await record_notification(db, candidate.user.id, subscription.id, _LEGACY_TRIAL_END_TYPE)
    return delivered


async def handle_post_trial_ladder(
    db: AsyncSession, send_message: Any, candidate: LifecycleCandidate, config: dict[str, Any], now: datetime
) -> bool:
    texts = get_texts(candidate.user.language)
    step = candidate.step
    percent = int(step.get('discount_percent', 0))

    if percent > 0:
        valid_hours = int(step.get('valid_hours', 24))
        offer = await upsert_discount_offer(
            db,
            user_id=candidate.user.id,
            subscription_id=candidate.subscription.id if candidate.subscription else None,
            notification_type=_LADDER_OFFER_TYPE,
            discount_percent=percent,
            bonus_amount_kopeks=0,
            valid_hours=valid_hours,
            effect_type='percent_discount',
        )
        message = render_template(
            config.get('message_template', ''),
            percent=percent,
            hours=valid_hours,
            date=format_local_datetime(offer.expires_at, '%d.%m.%Y %H:%M'),
        )
        rows = [
            [build_miniapp_or_callback_button(text='🎁 Получить скидку', callback_data=f'claim_discount_{offer.id}')],
            [_build_button('buy', texts)],
            [_build_button('balance_topup', texts)],
            [_build_button('support', texts)],
        ]
        keyboard = InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row[0] is not None])
    else:
        message = render_template(config.get('message_template_no_discount') or config.get('message_template', ''))
        keyboard = _keyboard_from_keys(['buy', 'balance_topup'], texts)

    return await deliver(send_message, user=candidate.user, text=message, keyboard=keyboard)
