"""port_storm_control_action

Parametriza la acción de storm-control por puerto -- hasta ahora estaba
hardcodeada (Cisco siempre "shutdown"+"trap"; Huawei siempre "block"+
"enable trap"). Se agregan:

* ``device_ports.storm_control_action`` (String, nullable) --
  "filter" (descarta el exceso, el puerto sigue arriba) o "shutdown"
  (el puerto se cae). Leído del running-config/current-configuration
  real del device, no de una tabla operacional.
* ``device_ports.storm_control_trap`` (Boolean, nullable) -- si manda
  trap SNMP, independiente de la acción anterior en ambos vendors.

Mismo criterio de nullable que ``r12msp17_port_storm_control`` -- None
cuando el parser no supo (nunca se leyó, o storm-control está
deshabilitado).

Revision ID: w17msp22_storm_action
Revises: v16msp21_svi_ipv4_secondary_list
Create Date: 2026-09-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "w17msp22_storm_action"
down_revision: Union[str, Sequence[str], None] = "v16msp21_svi_ipv4_secondary_list"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "device_ports",
        sa.Column("storm_control_action", sa.String(), nullable=True),
    )
    op.add_column(
        "device_ports",
        sa.Column("storm_control_trap", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("device_ports", "storm_control_trap")
    op.drop_column("device_ports", "storm_control_action")
