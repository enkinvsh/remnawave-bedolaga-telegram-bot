"""broadcast error summary: per-broadcast failure breakdown

Adds ``broadcast_history.error_summary`` — a nullable JSON blob
``{'counts': {error_key: int}, 'samples': [str]}`` populated when a Telegram
broadcast finishes with failures. Makes the "Total=283 / Sent=0 / Errors=250"
incident diagnosable from the cabinet instead of only the server logs.

Revision ID: 0097
Revises: 0096
Create Date: 2026-07-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0097'
down_revision: Union[str, None] = '0096'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col['name'] for col in inspector.get_columns('broadcast_history')}
    if 'error_summary' in columns:
        return

    op.add_column('broadcast_history', sa.Column('error_summary', sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {col['name'] for col in inspector.get_columns('broadcast_history')}
    if 'error_summary' not in columns:
        return

    op.drop_column('broadcast_history', 'error_summary')
