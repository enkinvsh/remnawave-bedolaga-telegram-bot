from datetime import datetime
from html import escape
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.services.email_layout import render_branded_email
from app.cabinet.services.email_unsubscribe import create_unsubscribe_token
from app.config import settings
from app.services.email_message import RenderedEmail
from app.utils.timezone import format_email_datetime


_EVENT_CAMPAIGNS: dict[str, str] = {
    'post_trial_ladder': 'email_post_trial',
    'expired_discount_wave2': 'email_winback_w2',
    'expired_discount_wave3': 'email_winback_w3',
}


def _cabinet_url() -> str:
    return settings.CABINET_URL.rstrip('/')


def _unsubscribe_url(user_id: int) -> str:
    token = quote(create_unsubscribe_token(user_id), safe='')
    return f'{_cabinet_url()}/email/unsubscribe?token={token}'


def _tracked_cabinet_url(campaign: str) -> str:
    return f'{_cabinet_url()}?campaign={campaign}&utm_source=email&utm_medium=email&utm_campaign={campaign}'


def _promo_headers(user_id: int) -> dict[str, str]:
    url = _unsubscribe_url(user_id)
    return {
        'List-Unsubscribe': f'<{url}>',
        'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click',
    }


def _unsubscribe_html(user_id: int) -> str:
    url = escape(_unsubscribe_url(user_id), quote=True)
    return f'<p style="margin:0;color:#64748b;font-size:12px;">Не хотите получать предложения? <a href="{url}" style="color:#64748b;">Отписаться</a></p>'


async def render_trial_ending_email(db: AsyncSession, hours_before: int) -> RenderedEmail:
    body = f'<p>Осталось около {hours_before} ч. Оформите подписку в личном кабинете, чтобы доступ не прервался.</p>'
    html = await render_branded_email(
        db,
        title='Пробный доступ скоро закончится',
        body_html=body,
        cta_text='Открыть личный кабинет',
        cta_url=_tracked_cabinet_url('email_trial_ending'),
    )
    return RenderedEmail(
        subject='Пробный доступ скоро закончится',
        html=html,
        text=f'Пробный доступ заканчивается через {hours_before} ч. Откройте личный кабинет: {_cabinet_url()}',
    )


async def render_lifecycle_discount_email(
    db: AsyncSession,
    *,
    notification_type: str,
    percent: int,
    expires_at: datetime,
    user_id: int,
) -> RenderedEmail:
    expires = format_email_datetime(expires_at)
    body = (
        '<p>Она применится автоматически при следующей оплате — ничего вводить не нужно.</p>'
        f'<p>Действует до {escape(expires)}.</p>'
    )
    subject = f'Скидка {percent}% уже активна'
    html = await render_branded_email(
        db,
        title=subject,
        body_html=body,
        cta_text='Открыть личный кабинет',
        cta_url=_tracked_cabinet_url(_EVENT_CAMPAIGNS.get(notification_type, 'email_lifecycle')),
        unsubscribe_html=_unsubscribe_html(user_id),
    )
    return RenderedEmail(subject, html, f'{subject} до {expires}. Личный кабинет: {_cabinet_url()}')
