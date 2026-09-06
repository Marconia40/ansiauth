"""add_global_config_running_config

RF-GLOBAL-01 (SRS §3.4) — "consultar configuración general" se entendía
como el dump completo de ``show running-config``/``display
current-configuration``, no solo el resumen estructurado ya existente
(hostname/version/rutas/acls). Agrega ``running_config`` (texto crudo) a
``device_global_config``.

Revision ID: m7msp12_running_config
Revises: l6msp11_global_config
Create Date: 2026-09-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "m7msp12_running_config"
down_revision: Union[str, Sequence[str], None] = "l6msp11_global_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "device_global_config",
        sa.Column("running_config", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("device_global_config", "running_config")
