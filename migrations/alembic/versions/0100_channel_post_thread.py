"""channel-post: optional forum message_thread_id

Adds ``channel_posts.message_thread_id`` (BIGINT NULL) so a post can target a
specific forum topic in a supergroup. Additive / multi-service: no seed rows,
no service-specific literals. Idempotent guard mirrors 0099.

Revision ID: 0100
Revises: 0099
Create Date: 2026-07-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0100'
down_revision: Union[str, None] = '0099'
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
    if not _has_column('channel_posts', 'message_thread_id'):
        op.add_column(
            'channel_posts',
            sa.Column('message_thread_id', sa.BigInteger(), nullable=True),
        )


def downgrade() -> None:
    if _has_column('channel_posts', 'message_thread_id'):
        op.drop_column('channel_posts', 'message_thread_id')
