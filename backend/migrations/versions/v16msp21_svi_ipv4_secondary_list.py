"""svi_ipv4_secondary_list

``ipv4_address_secondary`` en ``device_svis`` pasa de String (1 sola IP)
a JSON (lista) -- el usuario pidió poder tener varias direcciones IPv4
secundarias por SVI (mismo pedido que ya resolvió ``dhcp_relay_servers``
como lista, RF-INTERV-05). A diferencia de DHCP relay, el comando de
device es aditivo/quitable por dirección puntual, no full-replace (ver
``SVI.ipv4_address_secondary`` en ``app/models/svi.py``) -- pero el
shape de cache sigue el mismo patrón que ``dhcp_relay_servers``: lista
en JSON, poblada por el parser (``svi_parser.py``).

Mismo criterio que ``n8msp13_ntp_dns_log_lists`` para no perder el valor
ya cacheado: se envuelve como lista de 1 elemento, se recompone del todo
(con las demás direcciones que el parser nuevo ya sabe acumular) en el
próximo refresh.

Revision ID: v16msp21_svi_ipv4_secondary_list
Revises: u15msp20_audit_summary
Create Date: 2026-09-09 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "v16msp21_svi_ipv4_secondary_list"
down_revision: Union[str, Sequence[str], None] = "u15msp20_audit_summary"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    is_sqlite = op.get_bind().dialect.name == "sqlite"
    if is_sqlite:
        # SQLite no soporta ``ALTER COLUMN ... TYPE`` -- mismo criterio que
        # ``n8msp13_ntp_dns_log_lists``: sin datos que preservar (envs
        # SQLite son tests/dev, se recomponen en el próximo refresh).
        with op.batch_alter_table("device_svis") as batch_op:
            batch_op.drop_column("ipv4_address_secondary")
            batch_op.add_column(sa.Column("ipv4_address_secondary", sa.JSON(), nullable=True))
    else:
        op.execute(
            "ALTER TABLE device_svis "
            "ALTER COLUMN ipv4_address_secondary TYPE json USING "
            "(CASE WHEN ipv4_address_secondary IS NULL THEN NULL "
            "ELSE to_jsonb(ARRAY[ipv4_address_secondary]) END)"
        )


def downgrade() -> None:
    is_sqlite = op.get_bind().dialect.name == "sqlite"
    if is_sqlite:
        with op.batch_alter_table("device_svis") as batch_op:
            batch_op.drop_column("ipv4_address_secondary")
            batch_op.add_column(sa.Column("ipv4_address_secondary", sa.String(), nullable=True))
    else:
        op.execute(
            "ALTER TABLE device_svis "
            "ALTER COLUMN ipv4_address_secondary TYPE varchar USING "
            "(CASE WHEN ipv4_address_secondary IS NULL THEN NULL "
            "ELSE (ipv4_address_secondary->>0) END)"
        )
