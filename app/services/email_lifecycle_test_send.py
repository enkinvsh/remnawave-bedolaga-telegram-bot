from datetime import datetime, timedelta
from enum import StrEnum
from typing import assert_never

from sqlalchemy.ext.asyncio import AsyncSession

from app.cabinet.services.email_layout import (
    EMAIL_SERVICE_CTA_TEXT,
    email_service_cta_url,
    render_branded_email_if_enabled,
)
from app.cabinet.services.email_templates import EmailNotificationTemplates
from app.config import settings
from app.services.email_message import RenderedEmail
from app.services.lifecycle_email_messages import render_lifecycle_discount_email, render_trial_ending_email
from app.services.notification_delivery_service import NotificationType
from app.services.winback_oneoff_audience import Cohort
from app.services.winback_oneoff_messages import DiscountEmailSpec, render_discount_email, render_trial_email


class TestSendEvent(StrEnum):
    TRIAL_ENDING = 'trial_ending'
    POST_TRIAL = 'post_trial'
    WINBACK_WAVE = 'winback_wave'
    WINBACK_TRIAL_INVITE = 'winback_trial_invite'
    WINBACK_TRIAL_RESET = 'winback_trial_reset'
    TOPUP = 'topup'


async def render_test_email(
    db: AsyncSession,
    event: TestSendEvent,
    admin_id: int,
    now: datetime,
) -> RenderedEmail:
    match event:
        case TestSendEvent.TRIAL_ENDING:
            return await render_trial_ending_email(db, 2)
        case TestSendEvent.POST_TRIAL:
            return await render_lifecycle_discount_email(
                db, notification_type='post_trial_ladder', percent=10, expires_at=now + timedelta(hours=24), user_id=admin_id
            )
        case TestSendEvent.WINBACK_WAVE:
            return await render_discount_email(db, DiscountEmailSpec(25, now + timedelta(hours=72), admin_id))
        case TestSendEvent.WINBACK_TRIAL_INVITE:
            return await render_trial_email(db, Cohort.B, admin_id)
        case TestSendEvent.WINBACK_TRIAL_RESET:
            return await render_trial_email(db, Cohort.C, admin_id)
        case TestSendEvent.TOPUP:
            templates = EmailNotificationTemplates()
            context = {
                'amount_kopeks': 10000,
                'amount_rubles': 100,
                'new_balance_kopeks': 25000,
                'new_balance_rubles': 250,
                'formatted_amount': settings.format_price(10000),
                'formatted_balance': settings.format_price(25000),
            }
            template = templates.get_template(NotificationType.BALANCE_TOPUP, 'ru', context)
            if template is None:
                raise AssertionError('Balance top-up template is required')
            html = await render_branded_email_if_enabled(
                db,
                title=template['subject'],
                body_html=template['body_html'],
                content_html=templates.get_content_only_html(template['body_html']),
                cta_text=EMAIL_SERVICE_CTA_TEXT,
                cta_url=email_service_cta_url(),
                include_referral=True,
            )
            return RenderedEmail(template['subject'], html, template.get('body_text'))
        case unreachable:
            assert_never(unreachable)
