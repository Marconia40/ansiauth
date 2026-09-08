"""device_ssh_key_auth

Agrega soporte de autenticación por clave SSH privada, alternativa a
password, para Device -- motivada por una falla intermitente real
confirmada en vivo contra un Huawei real (f3r9s2): la autenticación por
password rechaza la password CORRECTA de forma esporádica y sin patrón
de concurrencia ("User password authentication failed" en el log del
device, con la misma password funcionando en llamadas adyacentes), un
comportamiento que no se reprodujo en 20/20 intentos seguidos usando
autenticación por clave contra el mismo device. Con un fleet de ~80
Huawei planeado, clave > password como método por defecto a futuro.

* ``devices.auth_method`` (String, not null, default "password") --
  "password" (de siempre) o "key". Server default cubre las filas
  existentes sin backfill.
* ``devices.encrypted_private_key`` (Text, nullable) -- solo poblado
  cuando auth_method == "key". ``encrypted_password`` sigue siendo not
  null siempre (se guarda un string vacío cifrado para devices por
  clave) para no tener que tocar los ~43 call-sites existentes de
  ``device.password`` en el dominio.

Revision ID: s13msp18_device_ssh_key_auth
Revises: r12msp17_port_storm_control
Create Date: 2026-09-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "s13msp18_device_ssh_key_auth"
down_revision: Union[str, Sequence[str], None] = "r12msp17_port_storm_control"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("auth_method", sa.String(), nullable=False, server_default="password"),
    )
    op.add_column(
        "devices",
        sa.Column("encrypted_private_key", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("devices", "encrypted_private_key")
    op.drop_column("devices", "auth_method")
