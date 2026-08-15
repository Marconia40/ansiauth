"""add_sites

Revision ID: 8b5d2c7a3e1f
Revises: e7f4a2b9c810
Create Date: 2026-05-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8b5d2c7a3e1f'
down_revision: Union[str, Sequence[str], None] = 'e7f4a2b9c810'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sites',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_site_name'),
    )
    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sites_name'), ['name'], unique=True)

    # Add nullable site_id to devices with a FK back to sites(id).
    # SET NULL on delete keeps existing devices intact if a site is removed —
    # but the API blocks deletion of non-empty sites with 409 Conflict.
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('site_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_devices_site_id',
            'sites',
            ['site_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index(batch_op.f('ix_devices_site_id'), ['site_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_devices_site_id'))
        batch_op.drop_constraint('fk_devices_site_id', type_='foreignkey')
        batch_op.drop_column('site_id')

    with op.batch_alter_table('sites', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sites_name'))
    op.drop_table('sites')
