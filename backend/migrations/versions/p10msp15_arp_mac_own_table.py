"""separate_arp_mac_into_own_table_and_sync_scope

El usuario pidió que ARP/MAC tengan su propio sync específico, separado
del de ``global_config`` -- no hacen falta para ninguna escritura (nunca
deberían pesar en ``reconciliar()``) y "pueden traer muchísima info", así
que no tiene sentido que se disparen junto con todo lo demás (alta de
device, refresh general). Compartir fila con algo sincronizado por un
camino distinto también era un riesgo real: ``Repository.add()`` hace
``session.merge()`` del objeto completo, así que el próximo sync de
``global_config`` (que ya no trae ARP/MAC) iba a pisar esas columnas con
NULL la primera vez que corriera.

Mueve ``arp_table``/``mac_table`` de ``device_global_config`` a una tabla
propia (``device_arp_mac``, mismo criterio singleton-por-device que el
resto) y agrega ``arp_mac_synced_at``/``arp_mac_sync_error`` a ``devices``
para que tengan su propio scope de sync (``"arp_mac"``), independiente de
``"global_config"`` y explícitamente afuera de ``"all"``.

Revision ID: p10msp15_arp_mac_own_table
Revises: o9msp14_arp_mac_cache
Create Date: 2026-09-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "p10msp15_arp_mac_own_table"
down_revision: Union[str, Sequence[str], None] = "o9msp14_arp_mac_cache"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_arp_mac",
        sa.Column("device", sa.String(), primary_key=True),
        sa.Column("arp_table", sa.JSON(), nullable=True),
        sa.Column("mac_table", sa.JSON(), nullable=True),
    )
    # Mover los datos ya cacheados en vez de perderlos -- evita que el
    # primer GET /arp post-migración vuelva vacío hasta el próximo sync.
    op.execute(
        "INSERT INTO device_arp_mac (device, arp_table, mac_table) "
        "SELECT device, arp_table, mac_table FROM device_global_config "
        "WHERE arp_table IS NOT NULL OR mac_table IS NOT NULL"
    )
    op.drop_column("device_global_config", "arp_table")
    op.drop_column("device_global_config", "mac_table")

    op.add_column("devices", sa.Column("arp_mac_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("devices", sa.Column("arp_mac_sync_error", sa.Text(), nullable=True))
    # Los devices que ya tenían arp_table/mac_table pobladas ya fueron
    # sincronizados alguna vez -- copiar el timestamp de global_config
    # como mejor aproximación disponible (no hay uno más preciso, el
    # error nunca se guardaba por separado).
    op.execute(
        "UPDATE devices SET arp_mac_synced_at = global_config_synced_at "
        "WHERE name IN (SELECT device FROM device_arp_mac)"
    )


def downgrade() -> None:
    op.add_column("device_global_config", sa.Column("arp_table", sa.JSON(), nullable=True))
    op.add_column("device_global_config", sa.Column("mac_table", sa.JSON(), nullable=True))
    op.execute(
        "UPDATE device_global_config g SET "
        "arp_table = a.arp_table, mac_table = a.mac_table "
        "FROM device_arp_mac a WHERE a.device = g.device"
    )
    op.drop_column("devices", "arp_mac_sync_error")
    op.drop_column("devices", "arp_mac_synced_at")
    op.drop_table("device_arp_mac")
