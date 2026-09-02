"""add_device_sync_metadata

Introduce el modelo cache-first para lecturas de VLAN/port:

* ``devices.{vlans,ports}_synced_at`` / ``{vlans,ports}_sync_error`` --
  metadata por-recurso que dice "cuándo fue la última sync exitosa contra
  el equipo" y "el último intento falló con este error". Timestamps
  separados porque VLAN y ports se sincronizan por caminos distintos
  (endpoints de refresh distintos, post-escritura toca solo el scope
  cambiado). Todos nullable: un device recién dado de alta arranca con
  ``synced_at=NULL`` hasta que la task ``sync_device_task`` termina.
* ``device_ports.{operational_up, speed, duplex}`` -- campos read-only
  que hoy solo se veían leyendo en vivo del device. Al pasar los GET a
  leer de cache, hay que persistirlos también, o se pierden en la UI
  entre refresh y refresh. La nota vieja de ``DevicePortModel`` decía
  explícito "no operational_up/speed/duplex (solo lectura, vienen del
  device en cada reconciliar())" -- esa decisión pierde sentido con GET
  cache-first, así que se revisa acá.

Revision ID: j4msp9_device_sync_metadata
Revises: i3msp8_device_vlans_ports
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "j4msp9_device_sync_metadata"
down_revision: Union[str, Sequence[str], None] = "i3msp8_device_vlans_ports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("vlans_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("vlans_sync_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("ports_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("ports_sync_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "device_ports",
        sa.Column("operational_up", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "device_ports",
        sa.Column("speed", sa.String(), nullable=True),
    )
    op.add_column(
        "device_ports",
        sa.Column("duplex", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("device_ports", "duplex")
    op.drop_column("device_ports", "speed")
    op.drop_column("device_ports", "operational_up")
    op.drop_column("devices", "ports_sync_error")
    op.drop_column("devices", "ports_synced_at")
    op.drop_column("devices", "vlans_sync_error")
    op.drop_column("devices", "vlans_synced_at")
