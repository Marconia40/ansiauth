"""add_device_groups

Revision ID: e7f4a2b9c810
Revises: c3a9b2e1f4d7
Create Date: 2026-05-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7f4a2b9c810'
down_revision: Union[str, Sequence[str], None] = 'c3a9b2e1f4d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'device_groups',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_device_group_name'),
    )
    with op.batch_alter_table('device_groups', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_device_groups_name'), ['name'], unique=True)

    op.create_table(
        'device_group_members',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('device_name', sa.String(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['device_name'], ['devices.name'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['device_groups.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('group_id', 'device_name', name='uq_group_member'),
    )
    with op.batch_alter_table('device_group_members', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_device_group_members_group_id'), ['group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_device_group_members_device_name'), ['device_name'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('device_group_members', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_device_group_members_device_name'))
        batch_op.drop_index(batch_op.f('ix_device_group_members_group_id'))
    op.drop_table('device_group_members')

    with op.batch_alter_table('device_groups', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_device_groups_name'))
    op.drop_table('device_groups')
