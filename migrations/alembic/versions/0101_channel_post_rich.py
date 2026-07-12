"""channel-post: is_rich flag (sendRichMessage path)

Adds ``channel_posts.is_rich`` (BOOLEAN NOT NULL default false) marking a post
published via Bot API ``sendRichMessage``. Additive / multi-service: no seeds,
no service-specific literals. Idempotent guard mirrors 0099/0100.

Revision ID: 0101
Revises: 0100
Create Date: 2026-07-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0101'
down_revision: Union[str, None] = '0100'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.columns '
            "WHERE table_schema = 'public' AND table_name = :t AND column_name = :c)"
        ),
        {'t': table_name, 'c': column_name},
    )
    return result.scalar()


def upgrade() -> None:
    if not _has_column('channel_posts', 'is_rich'):
        op.add_column(
            'channel_posts',
            sa.Column('is_rich', sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    if _has_column('channel_posts', 'is_rich'):
        op.drop_column('channel_posts', 'is_rich')
