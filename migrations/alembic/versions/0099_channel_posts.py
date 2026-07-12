"""channel-post: is_post_target flag + channel_posts history table

Adds the posting-allowlist flag on ``required_channels`` and a dedicated
``channel_posts`` history table for the cabinet channel-post feature. Generic /
multi-service: NO seed rows, NO service-specific literals.

* ``required_channels.is_post_target`` — marks a channel/group as a valid POST
  target. Orthogonal to ``is_active`` (mandatory-subscription enforcement).
* ``channel_posts`` — one row per publish attempt; unique ``idempotency_key``
  gives at-most-once send, ``(channel_id, created_at)`` index serves history.

Revision ID: 0099
Revises: 0098
Create Date: 2026-07-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = '0099'
down_revision: Union[str, None] = '0098'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.tables '
            "WHERE table_schema = 'public' AND table_name = :name)"
        ),
        {'name': table_name},
    )
    return result.scalar()


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
    if not _has_column('required_channels', 'is_post_target'):
        op.add_column(
            'required_channels',
            sa.Column('is_post_target', sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    if not _has_table('channel_posts'):
        op.create_table(
            'channel_posts',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('channel_id', sa.String(100), nullable=False),
            sa.Column('title', sa.String(255), nullable=True),
            sa.Column('message_text', sa.Text(), nullable=True),
            sa.Column('buttons_json', JSONB(), nullable=True),
            sa.Column('media_json', JSONB(), nullable=True),
            sa.Column('idempotency_key', sa.String(255), nullable=False),
            sa.Column('telegram_message_id', sa.BigInteger(), nullable=True),
            sa.Column('status', sa.String(20), nullable=False),
            sa.Column('error_code', sa.String(100), nullable=True),
            sa.Column('admin_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint('idempotency_key', name='uq_channel_posts_idempotency_key'),
            sa.Index('ix_channel_posts_channel_created', 'channel_id', 'created_at'),
        )


def downgrade() -> None:
    if _has_table('channel_posts'):
        op.drop_index('ix_channel_posts_channel_created', table_name='channel_posts')
        op.drop_table('channel_posts')

    if _has_column('required_channels', 'is_post_target'):
        op.drop_column('required_channels', 'is_post_target')
