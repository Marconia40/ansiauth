"""job_error_summary

Agrega ``jobs.error_summary`` (RF: "interpretar mejor lo que dicen los
equipos" -- plan de esta sesión) -- una frase corta y legible de POR QUÉ
falló un job, calculada por ``Orquestador._resumir_error()`` a partir de la
misma clasificación (``RetryDecision``) que ya decidía si convenía
reintentar, hasta ahora solo logueada y descartada. Ver docstring de
``Job.error_summary`` (``models/job.py``).

De paso, agrega ``jobs.parameters_summary`` -- bug real encontrado en esta
misma vuelta: ese campo se agregó en una sesión anterior (frase legible de
la INTENCIÓN de un request, ej. "Add route 192.168.100.0/24 ->
10.10.100.1") pero nunca se le agregó la columna ni el mapeo ORM
(``JobRepository._to_domain()``/``_to_orm()``) -- se perdía en cada
escritura a la base y volvía ``None`` en cualquier lectura real, aunque en
memoria (el mismo request que lo escribió) pareciera andar. Se cierra acá
porque ``error_summary`` necesita exactamente la misma plomería y dejar el
primero roto mientras se agrega el segundo sería inconsistente.

Revision ID: t14msp19_job_error_summary
Revises: s13msp18_device_ssh_key_auth
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "t14msp19_job_error_summary"
down_revision: Union[str, Sequence[str], None] = "s13msp18_device_ssh_key_auth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("parameters_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("error_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("jobs", "error_summary")
    op.drop_column("jobs", "parameters_summary")
