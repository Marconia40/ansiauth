"""add_device_logs_table_and_sync_scope

Pedido del usuario: un endpoint de logs "de la misma forma que las
tablas mac y arp" -- muestra el log buffer local del device (``show
logging`` en Cisco, ``display logbuffer`` en Huawei). Mismo criterio que
ARP/MAC (ver ``o9msp14_arp_mac_cache``/``p10msp15_arp_mac_own_table``):
tabla y scope de sync propios (``"logs"``), afuera de ``"all"`` -- no
hace falta para ninguna escritura y puede ser mucha info.

``log_output`` queda Text (no JSON) -- es texto crudo multi-línea, mismo
criterio que ``running_config``.

Revision ID: q11msp16_device_logs
Revises: p10msp15_arp_mac_own_table
Create Date: 2026-09-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "q11msp16_device_logs"
down_revision: Union[str, Sequence[str], None] = "p10msp15_arp_mac_own_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_logs",
        sa.Column("device", sa.String(), primary_key=True),
        sa.Column("log_output", sa.Text(), nullable=True),
    )
    op.add_column("devices", sa.Column("logs_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("devices", sa.Column("logs_sync_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("devices", "logs_sync_error")
    op.drop_column("devices", "logs_synced_at")
    op.drop_table("device_logs")
