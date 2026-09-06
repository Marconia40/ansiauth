"""port_storm_control_read

Expone el estado de storm-control en la lectura de puertos.
Hasta ahora era write-only (PATCH /ports/storm-control) — no habia
forma de preguntar "¿esta prendido? ¿en que %?" para un puerto dado.

* ``device_ports.storm_control_enabled`` (Boolean, nullable) --
  True/False conocido; None cuando el driver no supo (parser
  no matcheo la salida real del equipo, evitar inventar valor).
* ``device_ports.storm_control_threshold`` (Float, nullable) --
  Porcentaje 0-100. Puede ser None aun con enabled=True cuando el
  equipo tiene storm-control configurado en unidades no-percent
  (pps/bps); nuestro write path solo produce percent-form asi que
  eso solo ocurre con configs preexistentes.

Revision ID: r12msp17_port_storm_control
Revises: q11msp16_device_logs
Create Date: 2026-09-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "r12msp17_port_storm_control"
down_revision: Union[str, Sequence[str], None] = "q11msp16_device_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "device_ports",
        sa.Column("storm_control_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "device_ports",
        sa.Column("storm_control_threshold", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("device_ports", "storm_control_threshold")
    op.drop_column("device_ports", "storm_control_enabled")
