"""add promo email opt out

Revision ID: 0102
Revises: 0101
Create Date: 2026-07-21 23:45:01.657491

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0102'
down_revision: Union[str, None] = '0101'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('promo_emails_opt_out_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'promo_emails_opt_out_at')
