"""device_cache_fks

Agrega ``ForeignKey("devices.name", ondelete="CASCADE")`` en las 6
tablas satélite que hoy identifican al device por nombre sin
constraint de integridad referencial (``device_vlans``,
``device_ports``, ``device_svis``, ``device_global_config``,
``device_arp_mac``, ``device_logs``).

Antes de esta migración ``Inventory.deregister()`` solo borraba la
fila de ``devices`` -- las 6 tablas cache-first quedaban con datos
del device borrado para siempre. Peor todavía: al re-registrar un
device con el mismo nombre, la UI mostraba VLANs/puertos/ACLs del
device viejo hasta el próximo sync.

Con FK+CASCADE Postgres/SQLite (con ``PRAGMA foreign_keys=ON``, ya
prendido en ``init_db``) limpian el cache al borrar el device --
resuelve el bug de raíz sin código de aplicación nuevo. Cubre gratis
cualquier tabla satélite futura mientras le sumen la constraint.

``jobs.device`` y ``audit_logs.device`` quedan a propósito sin FK:
son historial (queremos conservar el nombre del device incluso después
de que se borre), mismo criterio que ``audit_logs.user`` y
``refresh_tokens.username`` -- string suelto sin constraint.

Revision ID: x18msp23_device_fks
Revises: u15msp20_job_rollback_err
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "x18msp23_device_fks"
down_revision: Union[str, Sequence[str], None] = "u15msp20_job_rollback_err"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (tabla_satélite, nombre_constraint) -- todas usan ``device`` como
# columna local y ``devices.name`` como target. Nombres de constraint
# siguen el patrón ``fk_<tabla>_device`` para que ``batch_op.drop_constraint``
# en el downgrade los encuentre sin ambigüedad.
_SATELLITE_TABLES: list[tuple[str, str]] = [
    ("device_vlans", "fk_device_vlans_device"),
    ("device_ports", "fk_device_ports_device"),
    ("device_svis", "fk_device_svis_device"),
    ("device_global_config", "fk_device_global_config_device"),
    ("device_arp_mac", "fk_device_arp_mac_device"),
    ("device_logs", "fk_device_logs_device"),
]


def upgrade() -> None:
    conn = op.get_bind()

    # Paso 1: limpiar huérfanos pre-existentes. Sin esto ``create_foreign_key``
    # falla en el ADD CONSTRAINT si hay filas que referencian devices que ya
    # no existen (posibles hoy porque ``Inventory.deregister()`` nunca limpió
    # estas tablas -- bug que esta migración cierra).
    for table, _ in _SATELLITE_TABLES:
        conn.execute(
            sa.text(
                f"DELETE FROM {table} "
                f"WHERE device NOT IN (SELECT name FROM devices)"
            )
        )

    # Paso 2: agregar la FK con CASCADE. ``batch_alter_table`` recrea la
    # tabla en SQLite (única forma de agregar FK ahí -- ver
    # a3c7e9d1f482_device_group_site_id para el mismo patrón). En Postgres
    # emite un ``ALTER TABLE ... ADD CONSTRAINT`` directo, sin rebuild.
    for table, fk_name in _SATELLITE_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.create_foreign_key(
                fk_name,
                "devices",
                ["device"],
                ["name"],
                ondelete="CASCADE",
            )


def downgrade() -> None:
    for table, fk_name in _SATELLITE_TABLES:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(fk_name, type_="foreignkey")
