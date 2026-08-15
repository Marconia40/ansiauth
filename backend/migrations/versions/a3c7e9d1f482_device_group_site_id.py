"""device_group_site_id

Revision ID: a3c7e9d1f482
Revises: 9e2a4c8b6f10
Create Date: 2026-05-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c7e9d1f482'
down_revision: Union[str, Sequence[str], None] = '9e2a4c8b6f10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('device_groups', schema=None) as batch_op:
        batch_op.add_column(sa.Column('site_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_device_groups_site_id',
            'sites',
            ['site_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index(batch_op.f('ix_device_groups_site_id'), ['site_id'], unique=False)

    # Backfill: any group whose members all live in the same site gets that
    # site_id. Mixed-site and empty groups are left with site_id=NULL — admins
    # need to clean them up before non-admin users can see them again.
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE device_groups
           SET site_id = (
               SELECT MIN(d.site_id)
                 FROM device_group_members m
                 JOIN devices d ON d.name = m.device_name
                WHERE m.group_id = device_groups.id
                  AND d.site_id IS NOT NULL
           )
         WHERE site_id IS NULL
           AND id IN (
               SELECT m.group_id
                 FROM device_group_members m
                 JOIN devices d ON d.name = m.device_name
                GROUP BY m.group_id
               HAVING MIN(d.site_id) = MAX(d.site_id)
                  AND MIN(d.site_id) IS NOT NULL
           )
    """))


def downgrade() -> None:
    with op.batch_alter_table('device_groups', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_device_groups_site_id'))
        batch_op.drop_constraint('fk_device_groups_site_id', type_='foreignkey')
        batch_op.drop_column('site_id')
