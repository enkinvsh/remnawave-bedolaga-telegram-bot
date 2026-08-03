"""locale overrides

Admin-editable replacements for bundled localization strings, one row per
(key, language). Deployment-level multi-tenancy (one instance + one DB per
client) makes the table implicitly per-tenant — deliberately no tenant column.

Revision ID: 0103
Revises: 0102
Create Date: 2026-08-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0103'
down_revision: Union[str, None] = '0102'
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


def upgrade() -> None:
    if not _has_table('locale_overrides'):
        op.create_table(
            'locale_overrides',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('key', sa.String(255), nullable=False),
            sa.Column('language', sa.String(8), nullable=False),
            sa.Column('value', sa.Text(), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint('key', 'language', name='uq_locale_overrides_key_lang'),
            sa.Index('ix_locale_overrides_key', 'key'),
        )


def downgrade() -> None:
    if _has_table('locale_overrides'):
        op.drop_index('ix_locale_overrides_key', table_name='locale_overrides')
        op.drop_table('locale_overrides')
