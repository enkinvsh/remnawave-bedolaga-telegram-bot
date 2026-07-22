"""Operator-safe output formatting for one-off win-back runs."""

from app.services.winback_oneoff_audience import Audience, Cohort, CohortTarget, TargetChannel
from app.services.winback_oneoff_messages import RenderedEmail


SAMPLE_SIZE = 10


def mask_id(value: int | None) -> str:
    return '-' if value is None else f'***{str(value)[-1]}'


def mask_email(value: str | None) -> str:
    if value is None:
        return '-'
    local, separator, domain = value.partition('@')
    return f'{local[:1]}***{separator}{domain}'


def print_preview(cohort: Cohort, recipient: str | None, delivered: bool) -> None:
    print(
        f'PREVIEW winback_oneoff cohort={cohort.value} recipient={mask_email(recipient)} sent={str(delivered).lower()}'
    )


def print_dry_run(audiences: tuple[Audience, ...], selected: tuple[CohortTarget, ...], rendered: RenderedEmail) -> None:
    print('DRY-RUN winback_oneoff')
    for audience in audiences:
        email_count = sum(target.channel is TargetChannel.EMAIL for target in audience.targets)
        telegram_count = sum(target.channel is TargetChannel.TELEGRAM for target in audience.targets)
        print(f'cohort_{audience.cohort.value}_email={email_count}')
        print(f'cohort_{audience.cohort.value}_telegram={telegram_count}')
        print(f'cohort_{audience.cohort.value}_already_sent_skipped={audience.already_sent}')
    planned_resets = sum(item.cohort in (Cohort.C, Cohort.D) for item in selected)
    print(f'planned_resets={planned_resets}')
    print(f'selected_targets={len(selected)}')
    for index, item in enumerate(selected[:SAMPLE_SIZE], start=1):
        target = item.target
        print(
            f'sample[{index}] cohort={item.cohort.value} user_id={mask_id(target.user.id)} '
            f'channel={target.channel.value} email={mask_email(target.user.email)} '
            f'telegram_id={mask_id(target.user.telegram_id)}'
        )
    print(f'email_subject={rendered.subject}')
    print(f'email_html_first_200={rendered.html[:200]}')
    print('No sends, resets, or offers written. Use --apply to execute.')
