"""Audience selection and delivery engine for the one-off win-back campaign."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Final, assert_never

import anyio
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.cabinet.services.email_layout import render_branded_email
from app.config import settings
from app.database.crud.discount_offer import mark_offer_claimed, upsert_discount_offer
from app.database.crud.lifecycle import release_send_reservation, reserve_send
from app.services.email_delivery_service import send_email
from app.services.lifecycle_email_service import _promo_headers, _unsubscribe_html
from app.services.winback_oneoff_audience import (
    EVENT_KEY,
    Audience,
    Channel,
    EmailTarget,
    Target,
    TargetChannel,
    TelegramTarget,
    select_audience,
)
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.utils.timezone import format_email_datetime


RATE_DELAY_SECONDS: Final = 0.3
RETRY_DELAY_SECONDS: Final = 2
SAMPLE_SIZE: Final = 10


@dataclass(frozen=True, slots=True)
class Options:
    apply: bool = False
    limit: int | None = None
    channel: Channel = Channel.BOTH
    percent: int = 25
    valid_hours: int = 72
    preview_to: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    html: str
    text: str


@dataclass(frozen=True, slots=True)
class RunResult:
    selected: int
    sent: int
    skipped_reserved: int
    preview_sent: bool


async def render_campaign_email(
    db: AsyncSession, percent: int, expires_at: datetime, user_id: int
) -> RenderedEmail:
    expires = format_email_datetime(expires_at)
    subject = f'Мы обновились — и подготовили вам скидку {percent}%'
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
        unsubscribe_html=_unsubscribe_html(user_id),
    )
    text = f'Скидка {percent}% уже активна и применится автоматически при следующей оплате. Действует до {expires}.'
    return RenderedEmail(subject, html, text)


async def activate_discount(db: AsyncSession, target: Target, options: Options, now: datetime) -> datetime:
    offer = await upsert_discount_offer(
        db,
        user_id=target.user.id,
        subscription_id=target.subscription_id,
        notification_type=EVENT_KEY,
        discount_percent=options.percent,
        bonus_amount_kopeks=0,
        valid_hours=options.valid_hours,
        effect_type='percent_discount',
    )
    expires_at: datetime = offer.__dict__['expires_at']
    target.user.promo_offer_discount_percent = options.percent
    target.user.promo_offer_discount_source = EVENT_KEY
    target.user.promo_offer_discount_expires_at = expires_at
    target.user.updated_at = now
    await mark_offer_claimed(
        db,
        offer,
        details={
            'context': 'lifecycle_email_activation',
            'discount_percent': options.percent,
            'discount_expires_at': expires_at.isoformat(),
        },
    )
    return expires_at


def _selected_targets(audience: Audience, options: Options) -> tuple[Target, ...]:
    match options.channel:
        case Channel.EMAIL:
            targets = tuple(target for target in audience.targets if target.channel is TargetChannel.EMAIL)
        case Channel.TELEGRAM:
            targets = tuple(target for target in audience.targets if target.channel is TargetChannel.TELEGRAM)
        case Channel.BOTH:
            targets = audience.targets
        case _ as unreachable:
            assert_never(unreachable)
    return targets[: options.limit] if options.limit is not None else targets


def _mask_id(value: int | None) -> str:
    return '-' if value is None else f'***{str(value)[-1]}'


def _mask_email(value: str | None) -> str:
    if value is None:
        return '-'
    local, separator, domain = value.partition('@')
    return f'{local[:1]}***{separator}{domain}'


async def _send_preview(db: AsyncSession, options: Options, now: datetime) -> RunResult:
    rendered = await render_campaign_email(db, options.percent, now + timedelta(hours=options.valid_hours), 0)
    delivered = await send_email(
        to=options.preview_to or '', subject=rendered.subject, html=rendered.html, text=rendered.text, headers=None
    )
    if not delivered:
        await anyio.sleep(RETRY_DELAY_SECONDS)
        delivered = await send_email(
            to=options.preview_to or '', subject=rendered.subject, html=rendered.html, text=rendered.text, headers=None
        )
    print(f'PREVIEW winback_oneoff recipient={_mask_email(options.preview_to)} sent={str(delivered).lower()}')
    return RunResult(0, 0, 0, delivered)


async def run_campaign(db: AsyncSession, options: Options, now: datetime | None = None) -> RunResult:
    current_time = now or datetime.now(UTC)
    if options.preview_to is not None:
        return await _send_preview(db, options, current_time)
    audience = await select_audience(db, current_time)
    selected = _selected_targets(audience, options)
    if not options.apply:
        email_count = sum(target.channel is TargetChannel.EMAIL for target in audience.targets)
        telegram_count = sum(target.channel is TargetChannel.TELEGRAM for target in audience.targets)
        rendered = await render_campaign_email(db, options.percent, current_time + timedelta(hours=options.valid_hours), 0)
        print('DRY-RUN winback_oneoff')
        print(f'audience_email={email_count}')
        print(f'audience_telegram={telegram_count}')
        print(f'already_sent_skipped={audience.already_sent}')
        print(f'selected_targets={len(selected)}')
        for index, target in enumerate(selected[:SAMPLE_SIZE], start=1):
            print(
                f'sample[{index}] user_id={_mask_id(target.user.id)} channel={target.channel.value} '
                f'email={_mask_email(target.user.email)} telegram_id={_mask_id(target.user.telegram_id)}'
            )
        print(f'email_subject={rendered.subject}')
        print(f'email_html_first_200={rendered.html[:200]}')
        print('No sends or offers written. Use --apply to execute.')
        return RunResult(len(selected), 0, 0, False)

    sent_count = 0
    skipped_reserved = 0
    async with create_bot() as bot:
        for index, target in enumerate(selected):
            if not await reserve_send(db, target.user.id, EVENT_KEY, 1):
                skipped_reserved += 1
                continue
            delivered = False
            try:
                expires_at = await activate_discount(db, target, options, current_time)
                match target:
                    case EmailTarget():
                        rendered = await render_campaign_email(db, options.percent, expires_at, target.user.id)
                        delivered = await send_email(
                            to=target.email,
                            subject=rendered.subject,
                            html=rendered.html,
                            text=rendered.text,
                            headers=_promo_headers(target.user.id),
                        )
                        if not delivered:
                            await anyio.sleep(RETRY_DELAY_SECONDS)
                            delivered = await send_email(
                                to=target.email,
                                subject=rendered.subject,
                                html=rendered.html,
                                text=rendered.text,
                                headers=_promo_headers(target.user.id),
                            )
                    case TelegramTarget():
                        expires = format_email_datetime(expires_at)
                        text = (
                            f'Мы обновились. Ваша персональная скидка {options.percent}% уже активна и применится '
                            f'автоматически при следующей оплате — действует до {expires}.'
                        )
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [build_miniapp_or_callback_button(text='Продлить', callback_data='menu_buy')]
                            ]
                        )
                        try:
                            await bot.send_message(chat_id=target.telegram_id, text=text, reply_markup=keyboard)
                        except TelegramRetryAfter:
                            await anyio.sleep(RETRY_DELAY_SECONDS)
                            await bot.send_message(chat_id=target.telegram_id, text=text, reply_markup=keyboard)
                        delivered = True
                    case _ as unreachable:
                        assert_never(unreachable)
            finally:
                if not delivered:
                    await release_send_reservation(db, target.user.id, EVENT_KEY, 1)
            if delivered:
                sent_count += 1
            if index < len(selected) - 1:
                await anyio.sleep(RATE_DELAY_SECONDS)
    print(f'APPLY winback_oneoff selected={len(selected)} sent={sent_count} skipped_reserved={skipped_reserved}')
    return RunResult(len(selected), sent_count, skipped_reserved, False)
