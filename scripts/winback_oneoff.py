# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
# How to run: python scripts/winback_oneoff.py [--cohort a|b|c|d|all] [--apply] [--limit N]
# ruff: noqa: E402
"""Container entrypoint for the safe one-off win-back campaign."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import anyio

from app.database.database import AsyncSessionLocal
from app.services.winback_oneoff import CohortSelection, Options, run_campaign
from app.services.winback_oneoff_audience import Channel


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError('value must be greater than zero')
    return value


def parse_args(argv: Sequence[str] | None = None) -> Options:
    parser = argparse.ArgumentParser(description='Safe one-off win-back campaign (dry-run by default).')
    parser.add_argument('--apply', action='store_true', help='Activate offers and send messages.')
    parser.add_argument('--limit', type=_positive_int, help='Cap the selected target count.')
    parser.add_argument('--channel', choices=[channel.value for channel in Channel], default=Channel.BOTH.value)
    parser.add_argument(
        '--cohort', choices=[cohort.value for cohort in CohortSelection], default=CohortSelection.ALL.value
    )
    parser.add_argument('--percent', type=_positive_int, default=25)
    parser.add_argument('--valid-hours', type=_positive_int, default=72)
    parser.add_argument('--preview-to', help='Send one sample email without touching campaign users.')
    args = parser.parse_args(argv)
    return Options(
        apply=args.apply,
        limit=args.limit,
        channel=Channel(args.channel),
        percent=args.percent,
        valid_hours=args.valid_hours,
        preview_to=args.preview_to,
        cohort=CohortSelection(args.cohort),
    )


async def _main(options: Options) -> None:
    async with AsyncSessionLocal() as db:
        await run_campaign(db, options)


def main(argv: Sequence[str] | None = None) -> None:
    anyio.run(_main, parse_args(argv))


if __name__ == '__main__':
    main()
