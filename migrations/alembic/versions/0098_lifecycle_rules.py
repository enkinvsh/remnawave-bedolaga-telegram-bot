"""lifecycle rules + send-log: DB-backed configurable lifecycle messaging

Adds two tables that form the foundation of the configurable lifecycle-messaging
engine (задача C1):

* ``lifecycle_rules`` — per-rule enabled flag + JSON config override. Replaces the
  on-disk ``data/notification_settings.json`` of ``NotificationSettingsService``.
  A row is an OVERRIDE over the code-side defaults registry
  (``app/services/lifecycle_rules.py``); an empty table = pure defaults, so
  existing consumers behave identically with no rows.
* ``lifecycle_message_log`` — one row per (user, rule, occurrence) send, with a
  UNIQUE constraint that lets the trigger loop (задача C2) dedup repeats.

Revision ID: 0098
Revises: 0097
Create Date: 2026-07-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = '0098'
down_revision: Union[str, None] = '0097'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'lifecycle_rules' not in tables:
        op.create_table(
            'lifecycle_rules',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('key', sa.String(64), nullable=False),
            sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('config', sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index('ix_lifecycle_rules_key', 'lifecycle_rules', ['key'], unique=True)

    if 'lifecycle_message_log' not in tables:
        op.create_table(
            'lifecycle_message_log',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                'user_id',
                sa.Integer(),
                sa.ForeignKey('users.id', ondelete='CASCADE'),
                nullable=False,
                index=True,
            ),
            sa.Column('rule_key', sa.String(64), nullable=False),
            sa.Column('occurrence', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint(
                'user_id',
                'rule_key',
                'occurrence',
                name='uq_lifecycle_log_user_rule_occurrence',
            ),
        )
        op.create_index(
            'ix_lifecycle_log_rule_sent',
            'lifecycle_message_log',
            ['rule_key', 'sent_at'],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if 'lifecycle_message_log' in tables:
        op.drop_index('ix_lifecycle_log_rule_sent', table_name='lifecycle_message_log')
        op.drop_table('lifecycle_message_log')

    if 'lifecycle_rules' in tables:
        op.drop_index('ix_lifecycle_rules_key', table_name='lifecycle_rules')
        op.drop_table('lifecycle_rules')
