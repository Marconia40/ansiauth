"""add_svis

RF-INTERV-01 a 09 — SVIs (interface Vlan{n} en Cisco, Vlanif{n} en Huawei).

* ``device_svis`` -- PK compuesta (vlan_id, device), mismo
  criterio que ``device_vlans``/``device_ports``. Guarda el estado
  deseado/aplicado (description, admin_up, ipv4/ipv6, acl_in/out,
  dhcp_relay_servers) y el observado read-only (operational_up).
* ``devices.svis_synced_at`` / ``_sync_error`` -- mismo par
  de metadata cache-first que ya existe para vlans/ports (front-data).

Revision ID: k5msp10_svis
Revises: j4msp9_device_sync_metadata
Create Date: 2026-09-02 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "k5msp10_svis"
down_revision: Union[str, Sequence[str], None] = "j4msp9_device_sync_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_svis",
        sa.Column("vlan_id", sa.Integer(), nullable=False),
        sa.Column("device", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("admin_up", sa.Boolean(), nullable=True),
        sa.Column("ipv4_address", sa.String(), nullable=True),
        sa.Column("ipv4_address_secondary", sa.String(), nullable=True),
        sa.Column("ipv6_address", sa.String(), nullable=True),
        sa.Column("acl_in", sa.String(), nullable=True),
        sa.Column("acl_out", sa.String(), nullable=True),
        sa.Column("dhcp_relay_servers", sa.JSON(), nullable=True),
        sa.Column("operational_up", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("vlan_id", "device"),
    )
    op.add_column(
        "devices",
        sa.Column("svis_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("svis_sync_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("devices", "svis_sync_error")
    op.drop_column("devices", "svis_synced_at")
    op.drop_table("device_svis")
