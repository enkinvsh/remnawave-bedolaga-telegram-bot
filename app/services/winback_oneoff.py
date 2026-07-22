"""Audience selection and delivery engine for the one-off win-back campaign."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, assert_never

import anyio
import structlog
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_factory import create_bot
from app.database.crud.lifecycle import release_send_reservation, reserve_send
from app.services.email_delivery_service import send_email
from app.services.lifecycle_email_service import _promo_headers
from app.services.winback_oneoff_audience import (
    Audience,
    Channel,
    Cohort,
    CohortTarget,
    EmailTarget,
    TargetChannel,
    TelegramTarget,
    event_key_for,
    select_audience,
)
from app.services.winback_oneoff_discount import DiscountSpec, activate_discount
from app.services.winback_oneoff_messages import (
    DiscountEmailSpec,
    RenderedEmail,
    TelegramMessage,
    render_discount_email as render_campaign_email,
    render_trial_email,
    trial_telegram_message,
)
from app.services.winback_oneoff_reporting import print_dry_run, print_preview
from app.services.winback_oneoff_reset import reset_trial
from app.utils.miniapp_buttons import build_miniapp_or_callback_button
from app.utils.timezone import format_email_datetime

logger = structlog.get_logger(__name__)


RATE_DELAY_SECONDS: Final = 0.3
RETRY_DELAY_SECONDS: Final = 2


class CohortSelection(StrEnum):
    A = 'a'
    B = 'b'
    C = 'c'
    D = 'd'
    ALL = 'all'


@dataclass(frozen=True, slots=True)
class Options:
    apply: bool = False
    limit: int | None = None
    channel: Channel = Channel.BOTH
    percent: int = 25
    valid_hours: int = 72
    preview_to: str | None = None
    cohort: CohortSelection = CohortSelection.ALL


@dataclass(frozen=True, slots=True)
class RunResult:
    selected: int
    sent: int
    skipped_reserved: int
    preview_sent: bool
    resets: int = 0
    skipped_reset: int = 0


def _cohorts(selection: CohortSelection) -> tuple[Cohort, ...]:
    match selection:
        case CohortSelection.A:
            return (Cohort.A,)
        case CohortSelection.B:
            return (Cohort.B,)
        case CohortSelection.C:
            return (Cohort.C,)
        case CohortSelection.D:
            return (Cohort.D,)
        case CohortSelection.ALL:
            return tuple(Cohort)
        case unreachable:
            assert_never(unreachable)


def _selected_targets(audiences: tuple[Audience, ...], options: Options) -> tuple[CohortTarget, ...]:
    selected: list[CohortTarget] = []
    for audience in audiences:
        for target in audience.targets:
            match options.channel:
                case Channel.EMAIL if target.channel is TargetChannel.EMAIL:
                    selected.append(CohortTarget(audience.cohort, target))
                case Channel.TELEGRAM if target.channel is TargetChannel.TELEGRAM:
                    selected.append(CohortTarget(audience.cohort, target))
                case Channel.BOTH:
                    selected.append(CohortTarget(audience.cohort, target))
                case Channel.EMAIL | Channel.TELEGRAM:
                    pass
                case unreachable:
                    assert_never(unreachable)
    return tuple(selected[: options.limit] if options.limit is not None else selected)


async def _send_email_target(target: EmailTarget, rendered: RenderedEmail) -> bool:
    delivered = await send_email(
        to=target.email,
        subject=rendered.subject,
        html=rendered.html,
        text=rendered.text,
        headers=_promo_headers(target.user.id),
    )
    if delivered:
        return True
    await anyio.sleep(RETRY_DELAY_SECONDS)
    return await send_email(
        to=target.email,
        subject=rendered.subject,
        html=rendered.html,
        text=rendered.text,
        headers=_promo_headers(target.user.id),
    )


async def _send_telegram_target(bot, target: TelegramTarget, message: TelegramMessage) -> bool:
    try:
        await bot.send_message(chat_id=target.telegram_id, text=message.text, reply_markup=message.keyboard)
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        logger.info('winback: telegram unreachable — марker kept, never retried', user_id=target.user.id, reason=str(exc))
    except TelegramRetryAfter:
        await anyio.sleep(RETRY_DELAY_SECONDS)
        try:
            await bot.send_message(chat_id=target.telegram_id, text=message.text, reply_markup=message.keyboard)
        except (TelegramForbiddenError, TelegramBadRequest) as exc:
            logger.info('winback: telegram unreachable — marker kept, never retried', user_id=target.user.id, reason=str(exc))
    return True


async def _deliver_trial(db: AsyncSession, bot, selected: CohortTarget) -> bool:
    match selected.target:
        case EmailTarget() as target:
            rendered = await render_trial_email(db, selected.cohort, target.user.id)
            return await _send_email_target(target, rendered)
        case TelegramTarget() as target:
            return await _send_telegram_target(bot, target, trial_telegram_message(selected.cohort))
        case unreachable:
            assert_never(unreachable)


async def _preview_email(db: AsyncSession, options: Options, now: datetime) -> tuple[Cohort, RenderedEmail]:
    cohort = _cohorts(options.cohort)[0]
    match cohort:
        case Cohort.A:
            rendered = await render_campaign_email(
                db,
                DiscountEmailSpec(options.percent, now + timedelta(hours=options.valid_hours), 0),
            )
        case Cohort.B | Cohort.C | Cohort.D:
            rendered = await render_trial_email(db, cohort, 0)
        case unreachable:
            assert_never(unreachable)
    return cohort, rendered


async def _send_preview(db: AsyncSession, options: Options, now: datetime) -> RunResult:
    cohort, rendered = await _preview_email(db, options, now)
    delivered = await send_email(
        to=options.preview_to or '', subject=rendered.subject, html=rendered.html, text=rendered.text, headers=None
    )
    if not delivered:
        await anyio.sleep(RETRY_DELAY_SECONDS)
        delivered = await send_email(
            to=options.preview_to or '', subject=rendered.subject, html=rendered.html, text=rendered.text, headers=None
        )
    print_preview(cohort, options.preview_to, delivered)
    return RunResult(0, 0, 0, delivered)


async def run_campaign(db: AsyncSession, options: Options, now: datetime | None = None) -> RunResult:
    current_time = now or datetime.now(UTC)
    if options.preview_to is not None:
        return await _send_preview(db, options, current_time)
    audiences = tuple([await select_audience(db, current_time, cohort) for cohort in _cohorts(options.cohort)])
    selected = _selected_targets(audiences, options)
    if not options.apply:
        _, rendered = await _preview_email(db, options, current_time)
        print_dry_run(audiences, selected, rendered)
        return RunResult(len(selected), 0, 0, False)

    sent_count = 0
    reset_count = 0
    skipped_reserved = 0
    skipped_reset = 0
    async with create_bot() as bot:
        for index, item in enumerate(selected):
            event_key = event_key_for(item.cohort)
            target = item.target
            if not await reserve_send(db, target.user.id, event_key, 1):
                skipped_reserved += 1
                continue
            delivered = False
            try:
                match item.cohort:
                    case Cohort.A:
                        expires_at = await activate_discount(
                            db,
                            target,
                            DiscountSpec(options.percent, options.valid_hours, current_time),
                        )
                        match target:
                            case EmailTarget():
                                rendered = await render_campaign_email(
                                    db,
                                    DiscountEmailSpec(options.percent, expires_at, target.user.id),
                                )
                                delivered = await _send_email_target(target, rendered)
                            case TelegramTarget():
                                expires = format_email_datetime(expires_at)
                                message = TelegramMessage(
                                    text=(
                                        f'Мы обновились. Ваша персональная скидка {options.percent}% уже активна '
                                        f'и применится автоматически при следующей оплате — действует до {expires}.'
                                    ),
                                    keyboard=InlineKeyboardMarkup(
                                        inline_keyboard=[
                                            [
                                                build_miniapp_or_callback_button(
                                                    text='Продлить', callback_data='menu_buy'
                                                )
                                            ]
                                        ]
                                    ),
                                )
                                delivered = await _send_telegram_target(bot, target, message)
                            case unreachable:
                                assert_never(unreachable)
                    case Cohort.B:
                        delivered = await _deliver_trial(db, bot, item)
                    case Cohort.C | Cohort.D:
                        if not await reset_trial(db, target.user.id):
                            skipped_reset += 1
                        else:
                            reset_count += 1
                            delivered = await _deliver_trial(db, bot, item)
                    case unreachable:
                        assert_never(unreachable)
            finally:
                if not delivered:
                    await release_send_reservation(db, target.user.id, event_key, 1)
            if delivered:
                sent_count += 1
            if index < len(selected) - 1:
                await anyio.sleep(RATE_DELAY_SECONDS)
    print(
        f'APPLY winback_oneoff selected={len(selected)} sent={sent_count} resets={reset_count} '
        f'skipped_reserved={skipped_reserved} skipped_reset={skipped_reset}'
    )
    return RunResult(len(selected), sent_count, skipped_reserved, False, reset_count, skipped_reset)
