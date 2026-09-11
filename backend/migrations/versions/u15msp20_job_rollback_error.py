"""job_rollback_error

Agrega ``jobs.rollback_error`` (Text, nullable) para persistir el
motivo crudo del fallo del rollback cuando ``rollback_success=false``.
Complementa a ``error`` (motivo del apply) -- antes de esta migración
el usuario veía ``rollback_success=false`` sin poder saber POR QUÉ
(device rechazó el revert, timeout SSH, verify per-recurso no
coincidió con el pre_state).

Revision ID: u15msp20_job_rollback_err
Revises: t14msp19_job_err_class
Create Date: 2026-09-09 00:00:00.000000

Nota: revision ID capped a 25 chars por el ``VARCHAR(32)`` del
``alembic_version`` de Postgres -- mismo criterio que la migración
anterior (ver docstring de t14msp19).

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "u15msp20_job_rollback_err"
down_revision: Union[str, Sequence[str], None] = "t14msp19_job_err_class"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("rollback_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "rollback_error")
