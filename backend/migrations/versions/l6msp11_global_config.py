"""add_global_config

RF-GLOBAL-01 a 09 (SRS §3.4) — Configuración Global.

* ``device_global_config`` -- singleton por device (PK = ``device`` solo,
  no compuesta como VLAN/Puerto/SVI). Guarda el estado deseado/aplicado
  (hostname, snmp_*, ntp/dns/log servers) y el observado read-only
  (device_version, snmp_enabled, routes, acls -- estos 2 últimos como JSON,
  no tablas propias, ver docstring de ``DeviceGlobalConfigModel``).
* ``devices.global_config_synced_at`` / ``_sync_error`` -- mismo par de
  metadata cache-first que ya existe para vlans/ports/svis (front-data).

Revision ID: l6msp11_global_config
Revises: k5msp10_svis
Create Date: 2026-09-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "l6msp11_global_config"
down_revision: Union[str, Sequence[str], None] = "k5msp10_svis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_global_config",
        sa.Column("device", sa.String(), nullable=False),
        sa.Column("hostname", sa.String(), nullable=True),
        sa.Column("device_version", sa.String(), nullable=True),
        sa.Column("snmp_enabled", sa.Boolean(), nullable=True),
        sa.Column("snmp_version", sa.String(), nullable=True),
        sa.Column("snmp_community", sa.String(), nullable=True),
        sa.Column("snmp_permission", sa.String(), nullable=True),
        sa.Column("ntp_server", sa.String(), nullable=True),
        sa.Column("dns_server", sa.String(), nullable=True),
        sa.Column("log_server", sa.String(), nullable=True),
        sa.Column("log_level", sa.String(), nullable=True),
        sa.Column("routes", sa.JSON(), nullable=True),
        sa.Column("acls", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("device"),
    )
    op.add_column(
        "devices",
        sa.Column("global_config_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("global_config_sync_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("devices", "global_config_sync_error")
    op.drop_column("devices", "global_config_synced_at")
    op.drop_table("device_global_config")
