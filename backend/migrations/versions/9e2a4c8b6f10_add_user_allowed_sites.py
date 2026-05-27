"""add_user_allowed_sites

Revision ID: 9e2a4c8b6f10
Revises: 8b5d2c7a3e1f
Create Date: 2026-05-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9e2a4c8b6f10'
down_revision: Union[str, Sequence[str], None] = '8b5d2c7a3e1f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'user_allowed_sites',
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('site_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('user_id', 'site_id'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['site_id'], ['sites.id'], ondelete='CASCADE'),
    )
    with op.batch_alter_table('user_allowed_sites', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_allowed_sites_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_allowed_sites_site_id'), ['site_id'], unique=False)

    # Backfill: existing non-admin users get access to every existing site,
    # so the policy switch is non-breaking. Admins / super-admins bypass scoping
    # at the application layer and don't need rows here.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("""
            INSERT INTO user_allowed_sites (user_id, site_id, created_at)
            SELECT u.id, s.id, CURRENT_TIMESTAMP
              FROM users u
             CROSS JOIN sites s
             WHERE u.role NOT IN ('admin', 'super-admin')
               AND NOT EXISTS (
                   SELECT 1 FROM user_allowed_sites x
                    WHERE x.user_id = u.id AND x.site_id = s.id
               )
        """)
    )
    # rowcount may be -1 on some drivers; just log the attempt
    print(f"backfilled user_allowed_sites: rowcount={getattr(rows, 'rowcount', '?')}")


def downgrade() -> None:
    with op.batch_alter_table('user_allowed_sites', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_allowed_sites_site_id'))
        batch_op.drop_index(batch_op.f('ix_user_allowed_sites_user_id'))
    op.drop_table('user_allowed_sites')
