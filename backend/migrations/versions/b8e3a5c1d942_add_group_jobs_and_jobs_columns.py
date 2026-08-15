"""add_group_jobs_table_and_jobs_columns

Brings Alembic in line with the schema that was previously created at startup
by ``Base.metadata.create_all()`` and the ``_migrate_rollback_success`` /
``_migrate_group_job_id`` shims in ``app/main.py``:

* Creates the ``group_jobs`` table (never had its own migration).
* Adds ``jobs.rollback_success`` (was added at startup).
* Adds ``jobs.group_job_id`` + its index (was added at startup).
* Adds the composite ``ix_jobs_status_created_at`` index declared on
  ``JobModel.__table_args__`` (was only ever materialised via ``create_all``).

Revision ID: b8e3a5c1d942
Revises: a3c7e9d1f482
Create Date: 2026-06-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8e3a5c1d942'
down_revision: Union[str, Sequence[str], None] = 'a3c7e9d1f482'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'group_jobs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('group_job_id', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('operation', sa.String(), nullable=True),
        sa.Column('playbook', sa.String(), nullable=True),
        sa.Column('parameters', sa.JSON(), nullable=True),
        sa.Column('device_results', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('group_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_group_jobs_group_job_id'), ['group_job_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_group_jobs_status'), ['status'], unique=False)

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rollback_success', sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column('group_job_id', sa.String(), nullable=True))
        batch_op.create_index(batch_op.f('ix_jobs_group_job_id'), ['group_job_id'], unique=False)
        batch_op.create_index('ix_jobs_status_created_at', ['status', 'created_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_index('ix_jobs_status_created_at')
        batch_op.drop_index(batch_op.f('ix_jobs_group_job_id'))
        batch_op.drop_column('group_job_id')
        batch_op.drop_column('rollback_success')

    with op.batch_alter_table('group_jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_group_jobs_status'))
        batch_op.drop_index(batch_op.f('ix_group_jobs_group_job_id'))
    op.drop_table('group_jobs')
