"""job_error_classification

Agrega 3 columnas al ``jobs`` para persistir la clasificación amigable
del error final de un job. El ``error`` crudo (Text) sigue existiendo
para debug -- estas 3 son el resumen legible que ``GET /jobs/{id}``
devuelve al frontend sin que haya que re-derivar la clasificación cada
vez con las tablas de ``Orquestador._clasificar_error``.

* ``jobs.error_type`` (String, nullable) -- "permanent" | "transient" |
  "unknown". Mismo eje que ``RetryDecision.classification``. NULL para
  jobs completados con éxito y para jobs generados antes de esta
  migración.
* ``jobs.error_reason`` (String, nullable) -- patrón crudo que matcheó
  en las tablas de patterns (ej. "invalid input", "unable to open
  channel"). Útil para tunear patterns con datos reales agregados en
  producción.
* ``jobs.error_summary`` (Text, nullable) -- mensaje corto en inglés
  para render en UI (ej. "Command not supported by this device").
  Text por si algún caso necesita más de VARCHAR de largo default.

Revision ID: t14msp19_job_err_class
Revises: s13msp18_device_ssh_key_auth
Create Date: 2026-09-09 00:00:00.000000

Note: revision ID capped at 22 chars because ``alembic_version.version_num``
is ``VARCHAR(32)`` in Postgres (Alembic's default) -- el nombre largo
original (``t14msp19_job_error_classification``, 33 chars) rompe el
``UPDATE alembic_version`` con ``StringDataRightTruncation``. Nombre del
archivo se mantiene descriptivo -- es solo la revision string la que
tiene el límite.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "t14msp19_job_err_class"
down_revision: Union[str, Sequence[str], None] = "s13msp18_device_ssh_key_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("error_type", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("error_reason", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("error_summary", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "error_summary")
    op.drop_column("jobs", "error_reason")
    op.drop_column("jobs", "error_type")
