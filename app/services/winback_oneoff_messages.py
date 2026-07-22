"""Message rendering for the one-off win-back cohorts."""

from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import assert_never

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.services.email_layout import render_branded_email
from app.config import settings
from app.keyboards.inline import get_trial_keyboard
from app.services.email_message import RenderedEmail
from app.services.lifecycle_email_messages import _tracked_cabinet_url, _unsubscribe_html
from app.services.winback_oneoff_audience import Cohort
from app.utils.timezone import format_email_datetime


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    text: str
    keyboard: InlineKeyboardMarkup


@dataclass(frozen=True, slots=True)
class DiscountEmailSpec:
    percent: int
    expires_at: datetime
    user_id: int


async def render_discount_email(db: AsyncSession, spec: DiscountEmailSpec) -> RenderedEmail:
    expires = format_email_datetime(spec.expires_at)
    subject = f'Мы обновились — и подготовили вам скидку {spec.percent}%'
    body = (
        '<p>Скидка уже активна в вашем аккаунте и применится автоматически при следующей оплате. '
        f'Действует до {escape(expires)}.</p>'
    )
    html = await render_branded_email(
        db,
        title=subject,
        body_html=body,
        cta_text='Открыть личный кабинет',
        cta_url=settings.CABINET_URL.rstrip('/'),
        unsubscribe_html=_unsubscribe_html(spec.user_id),
    )
    text = (
        f'Скидка {spec.percent}% уже активна и применится автоматически при следующей оплате. Действует до {expires}.'
    )
    return RenderedEmail(subject, html, text)


def trial_message(cohort: Cohort) -> str:
    match cohort:
        case Cohort.B:
            return 'Мы обновили сервис. Пробный период ждёт активации — это займёт меньше минуты.'
        case Cohort.C | Cohort.D:
            return 'Мы обновили сервис и сбросили ваш пробный период — его можно активировать заново.'
        case Cohort.A:
            raise AssertionError('Discount cohort has no trial message')
        case unreachable:
            assert_never(unreachable)


async def render_trial_email(db: AsyncSession, cohort: Cohort, user_id: int) -> RenderedEmail:
    subject = 'Новый пробный период доступен'
    message = trial_message(cohort)
    html = await render_branded_email(
        db,
        title=subject,
        body_html=f'<p>{message}</p>',
        cta_text='Активировать пробный период',
        cta_url=_tracked_cabinet_url('winback_trial'),
        unsubscribe_html=_unsubscribe_html(user_id),
    )
    return RenderedEmail(subject, html, message)


def trial_telegram_message(cohort: Cohort) -> TelegramMessage:
    return TelegramMessage(trial_message(cohort), get_trial_keyboard('ru'))
