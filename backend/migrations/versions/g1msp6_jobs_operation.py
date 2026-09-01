"""add_jobs_operation_column

Single-purpose migration, scoped exception to this migration plan's "no
Alembic migrations" decision: JobModel.operation (added in
docs/migracion-final-architecture/FASE_4.md A1/A3, JobRepository) is read
by app/main.py's startup path (job_service.mark_orphaned_jobs_failed(),
inherited from the pre-migration codebase, still the real path until
Fase 5) via a plain ``SELECT jobs.*`` — without this column, app.main
fails to import against any Alembic-migrated database, not just tests.
Every other new column/table this plan adds (DeviceVlanModel,
DevicePortModel, ...) stays intentionally unmigrated because nothing in
the pre-migration codebase queries them yet; this one is different
because JobModel is shared with code that's still live.

Revision ID: g1msp6_jobs_op
Revises: f6msp5_rls
Create Date: 2026-08-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "g1msp6_jobs_op"
down_revision: Union[str, Sequence[str], None] = "f6msp5_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("operation", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("operation")
