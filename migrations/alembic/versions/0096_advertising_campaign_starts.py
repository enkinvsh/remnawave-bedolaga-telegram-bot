"""advertising campaign starts: raw /start + cabinet-visit events

Adds ``advertising_campaign_starts`` — one row per campaign-tagged hit (bot
``/start`` OR cabinet visit), recorded for EVERY user (new and existing).
Unlike ``advertising_campaign_registrations`` (deduped, new users only), these
events are raw: uniqueness is computed at query time. Closes the "existing user
via ad link leaves no trace" gap that showed zeros in the campaign dashboards.

Revision ID: 0096
Revises: 0095
Create Date: 2026-07-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0096'
down_revision: Union[str, None] = '0095'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'advertising_campaign_starts' in inspector.get_table_names():
        return

    op.create_table(
        'advertising_campaign_starts',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            'campaign_id',
            sa.Integer(),
            sa.ForeignKey('advertising_campaigns.id', ondelete='CASCADE'),
            nullable=False,
            index=True,
        ),
        sa.Column('telegram_id', sa.BigInteger(), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('source', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(
        'ix_campaign_start_campaign_created',
        'advertising_campaign_starts',
        ['campaign_id', 'created_at'],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'advertising_campaign_starts' not in inspector.get_table_names():
        return

    op.drop_index('ix_campaign_start_campaign_created', table_name='advertising_campaign_starts')
    op.drop_table('advertising_campaign_starts')
