"""add_device_vlans_and_device_ports

Bug real de producción, no un backlog item: ``device_vlans``/``device_ports``
(``DeviceVlanModel``/``DevicePortModel``, app/db/models.py) nunca tuvieron
una migración Alembic real -- solo existían vía ``Base.metadata.
create_all()``, que la app real (Celery worker en producción, corriendo
contra Postgres real, no el SQLite de desarrollo que sí las crea al vuelo
en algunos scripts de verificación) nunca llama. Confirmado con un log de
producción real: crear una VLAN aplicaba bien contra el device (Ansible
``rc=0``, ``changed``), pero ``Orquestador.ejecutar()``'s
``self._repos["vlan"].add(recurso)`` (el tracking-write posterior al
cambio real) tiraba ``psycopg.errors.UndefinedTable: relation
"device_vlans" does not exist``. Ese gap ya se había encontrado y
documentado como "fuera de alcance" en una sesión anterior; ahora hay
evidencia real de que rompe producción, así que se arregla acá.

Revision ID: i3msp8_device_vlans_ports
Revises: h2msp7_drop_gj
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "i3msp8_device_vlans_ports"
down_revision: Union[str, Sequence[str], None] = "h2msp7_drop_gj"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_vlans",
        sa.Column("vlan_id", sa.Integer(), nullable=False),
        sa.Column("device", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("vlan_id", "device"),
    )
    op.create_table(
        "device_ports",
        sa.Column("interface", sa.String(), nullable=False),
        sa.Column("device", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("admin_up", sa.Boolean(), nullable=True),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column("access_vlan", sa.Integer(), nullable=True),
        sa.Column("allowed_vlans", sa.JSON(), nullable=True),
        sa.Column("poe_enabled", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("interface", "device"),
    )


def downgrade() -> None:
    op.drop_table("device_ports")
    op.drop_table("device_vlans")
