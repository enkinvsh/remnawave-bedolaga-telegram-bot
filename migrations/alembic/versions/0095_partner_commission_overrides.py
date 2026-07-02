"""partner commission overrides: per-partner first-payment % and recurring tier ladder

Adds two nullable columns to ``users`` so an admin can override referral
commission per partner:

- ``referral_first_payment_percent`` (int): flat % on the referral's first payment.
- ``referral_recurring_tiers`` (text): per-partner recurring ladder in the same
  ``threshold:percent,...`` format as the global ``REFERRAL_RECURRING_COMMISSION_TIERS``
  setting, keyed by the partner's paid-referral count.

NULL/empty on either column means "inherit the existing global behavior", so
partners without overrides are unaffected (full backward compatibility).

Revision ID: 0095
Revises: 0094
Create Date: 2026-06-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0095'
down_revision: Union[str, None] = '0094'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c['name'] for c in inspector.get_columns('users')}
    if 'referral_first_payment_percent' not in existing:
        op.add_column('users', sa.Column('referral_first_payment_percent', sa.Integer(), nullable=True))
    if 'referral_recurring_tiers' not in existing:
        op.add_column('users', sa.Column('referral_recurring_tiers', sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c['name'] for c in inspector.get_columns('users')}
    if 'referral_recurring_tiers' in existing:
        op.drop_column('users', 'referral_recurring_tiers')
    if 'referral_first_payment_percent' in existing:
        op.drop_column('users', 'referral_first_payment_percent')
