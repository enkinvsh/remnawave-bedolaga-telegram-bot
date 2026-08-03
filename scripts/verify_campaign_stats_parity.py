# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
# How to run: python scripts/verify_campaign_stats_parity.py
# ruff: noqa: E402
"""Prove the batched campaigns-list stats equal the per-campaign oracle, campaign by campaign.

`get_campaign_statistics` is the oracle: it is the code that produced today's numbers and it is
deliberately left untouched. Exit code 1 means the admin list would render at least one campaign
differently than before.
"""

import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import anyio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.campaign import get_campaign_statistics, get_campaigns_page_with_stats
from app.database.database import AsyncSessionLocal
from app.database.models import AdvertisingCampaign


_COMPARED_FIELDS = (
    'registrations',
    'total_revenue_kopeks',
    'paid_users_count',
    'conversion_rate',
)
_SAMPLE_SIZE = 20


async def _collect_mismatches(db: AsyncSession) -> tuple[int, list[dict[str, object]]]:
    # REPEATABLE READ: without it a deposit landing between the batched query and the
    # per-campaign oracle reads as a parity failure that no code change caused.
    await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))

    total_campaigns = (await db.execute(select(func.count(AdvertisingCampaign.id)))).scalar_one() or 0
    if not total_campaigns:
        return 0, []

    rows, _ = await get_campaigns_page_with_stats(db, offset=0, limit=total_campaigns, include_inactive=True)

    mismatches: list[dict[str, object]] = []
    for row in rows:
        oracle = await get_campaign_statistics(db, row['id'])
        # Exact equality, no epsilon: both sides round in Python, so any difference is a
        # design error — and an epsilon would hide exactly the drift this script exists to catch.
        differing = {
            field: {'oracle': oracle[field], 'batched': row[field]}
            for field in _COMPARED_FIELDS
            if oracle[field] != row[field]
        }
        if differing:
            mismatches.append({'campaign_id': row['id'], 'name': row['name'], 'fields': differing})

    return len(rows), mismatches


async def _main() -> int:
    async with AsyncSessionLocal() as db:
        checked, mismatches = await _collect_mismatches(db)

    print(
        json.dumps(
            {'checked': checked, 'mismatches': len(mismatches), 'sample': mismatches[:_SAMPLE_SIZE]},
            ensure_ascii=False,
            default=str,
            indent=2,
        )
    )
    return 1 if mismatches else 0


def main() -> None:
    raise SystemExit(anyio.run(_main))


if __name__ == '__main__':
    main()
