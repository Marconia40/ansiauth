"""add_global_config_arp_mac_cache

RF-GLOBAL-01..09 no pedían ARP/MAC -- se agregaron fuera de alcance a
pedido del usuario, y quedaron como la ÚNICA lectura 100% en vivo de toda
la app (sin cache, ver commands.yaml/driver -- decisión explícita de esa
vuelta: la tabla cambia todo el tiempo, cachearla la volvería vieja al
instante). El usuario después notó que cada `GET /arp`/`GET /mac` paga el
overhead completo de una sesión SSH/Ansible nueva (mismo costo que hace
que un sync de Huawei tarde ~19s) y pidió sumarlas al cache/sync como
todo lo demás, aceptando el trade-off de "desactualizado hasta el próximo
sync" a cambio de que el GET sea instantáneo.

Agrega ``arp_table``/``mac_table`` (JSON, lista de dict) a
``device_global_config`` -- se leen COMPLETAS (sin filtro) durante el
sync; el filtro ``include`` de la API ahora se aplica en Python sobre
estos datos ya cacheados en vez de como un `| include` en el device.

Revision ID: o9msp14_arp_mac_cache
Revises: n8msp13_ntp_dns_log_lists
Create Date: 2026-09-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "o9msp14_arp_mac_cache"
down_revision: Union[str, Sequence[str], None] = "n8msp13_ntp_dns_log_lists"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("device_global_config", sa.Column("arp_table", sa.JSON(), nullable=True))
    op.add_column("device_global_config", sa.Column("mac_table", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("device_global_config", "mac_table")
    op.drop_column("device_global_config", "arp_table")
