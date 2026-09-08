"""audit_summary

Agrega ``audit_logs.summary`` -- plan de esta sesión ("Normalizar el audit
log de operaciones de device"). Frase corta y legible de qué se hizo (o por
qué falló), calculada por ``AuditRecord._resumir()`` a partir de
``RecursoGestionable.resumen_intento()`` (ya existe, agregado en una vuelta
anterior de esta misma sesión para el job detail modal) y
``error_summary`` (idem, ``Orquestador._resumir_error()``) -- reemplaza
tener que leer el ``details`` crudo (rc/stdout/stderr, a veces el
transcript SSH completo) para entender una fila del audit log. Acotado a
``vlan``/``puerto``/``svi``/``global_config`` (los únicos recursos con
``resumen_intento()``) -- filas de ``auth``/``users``/``sites``/etc. quedan
con ``summary=NULL``, sin cambio de comportamiento.

Revision ID: u15msp20_audit_summary
Revises: t14msp19_job_error_summary
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "u15msp20_audit_summary"
down_revision: Union[str, Sequence[str], None] = "t14msp19_job_error_summary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audit_logs",
        sa.Column("summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_logs", "summary")
