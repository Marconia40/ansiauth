"""add_global_config_ntp_dns_log_lists_and_snmp_trap_host

Feedback tras probar la vuelta anterior en vivo: ``ntp.server``/
``dns.server``/``logging.server`` mostraban solo el PRIMER valor
encontrado en el ``running_config`` -- el usuario pidió la lista completa
(puede haber más de 1, ej. varios ``ip name-server``/``ntp server``).
Renombra ``ntp_server``/``dns_server``/``log_server`` (String, 1 valor) a
``ntp_servers``/``dns_servers``/``log_servers`` (JSON, lista) -- los
valores existentes en cache se preservan como lista de 1 elemento, se
recomponen del todo en el próximo refresh.

También agrega ``snmp_trap_hosts`` (JSON, lista -- puede haber más de 1
host: Cisco puede tener varias líneas "snmp-server host", y Huawei expone
TODOS los hosts permitidos por la ACL atada al agente, no solo el
primero). Cisco: leído de "snmp-server host {ip} version {v}
{community}", la misma línea que ya se usaba para ``snmp_version``.
Huawei: derivado de "snmp-agent acl {nombre}" cruzado contra ``acls`` --
ver notas en ``global_config_parser.py``.

Además, ``acls`` (ya era JSON, sin cambio de columna) cambia de forma --
antes lista de str (nombres), ahora lista de dict (nombre/tipo/reglas,
"la acl debería especificar el contenido de cada una"). No hay forma de
reconstruir las reglas de un valor ya cacheado en el shape viejo (el
contenido real nunca se guardó), así que esta migración pone en ``NULL``
cualquier ``acls`` existente para evitar que ``GET /global-config/``
rompa la validación del schema nuevo contra datos con el shape viejo --
se recompone solo, con contenido real, en el próximo refresh (igual que
cualquier otro dato de este cache).

Revision ID: n8msp13_ntp_dns_log_lists
Revises: m7msp12_running_config
Create Date: 2026-09-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "n8msp13_ntp_dns_log_lists"
down_revision: Union[str, Sequence[str], None] = "m7msp12_running_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RENAMES = (
    ("ntp_server", "ntp_servers"),
    ("dns_server", "dns_servers"),
    ("log_server", "log_servers"),
)


def upgrade() -> None:
    op.add_column(
        "device_global_config",
        sa.Column("snmp_trap_hosts", sa.JSON(), nullable=True),
    )
    for old, new in _RENAMES:
        op.execute(
            f"ALTER TABLE device_global_config "
            f"ALTER COLUMN {old} TYPE json USING "
            f"(CASE WHEN {old} IS NULL THEN NULL ELSE to_jsonb(ARRAY[{old}]) END)"
        )
        op.alter_column("device_global_config", old, new_column_name=new)
    op.execute("UPDATE device_global_config SET acls = NULL WHERE acls IS NOT NULL")


def downgrade() -> None:
    for old, new in _RENAMES:
        op.alter_column("device_global_config", new, new_column_name=old)
        op.execute(
            f"ALTER TABLE device_global_config "
            f"ALTER COLUMN {old} TYPE varchar USING "
            f"(CASE WHEN {old} IS NULL THEN NULL ELSE ({old}->>0) END)"
        )
    op.drop_column("device_global_config", "snmp_trap_hosts")
