"""refresh_token_session_hardening

Amplía ``refresh_tokens`` con las columnas que hacen falta para el
endurecimiento de sesiones descrito en ``fix/jwt-and-time-sessions``:

- ``last_used_at``          → idle timeout server-side. La fila se
                              actualiza a ``now()`` en cada rotación; el
                              service rechaza el refresh si
                              ``now - last_used_at > REFRESH_TOKEN_IDLE_MINUTES``.
- ``session_id``            → UUID compartido por toda la cadena rotada
                              del mismo login. Permite revocar sólo "la
                              sesión actual" sin tumbar las demás
                              pestañas/dispositivos del usuario, y sirve
                              de anchor para el endpoint futuro de
                              "sesiones activas" (PR #4).
- ``session_started_at``    → anchor del absolute lifetime (SESSION_ABSOLUTE_MAX_HOURS).
                              Se propaga sin cambios en cada rotación,
                              cortando la sesión aunque el usuario esté
                              activo.
- ``parent_id``             → self-FK a la fila anterior de la cadena.
                              No lo usa la ruta caliente; queda para
                              auditoría/forensic ("¿cuántas veces rotó
                              esta sesión?").
- ``ip_address / user_agent`` → capturados en cada rotación para la UI
                              "Sesiones activas" del PR #4.

Backfill: las filas existentes se tratan como si el login hubiese sido
al momento de ``created_at`` y hasta ahora inactivas. Cada una recibe
un ``session_id`` propio (UUID nuevo) porque no hay forma retroactiva
de saber si pertenecían a la misma cadena. Aceptable: son tokens con
TTL de 4h del esquema viejo, decaerán en pocas horas.

SQLite (dev) usa ``lower(hex(randomblob(16)))``; Postgres usa
``gen_random_uuid()`` (extensión ``pgcrypto`` — ya cargada por
``e2msp2_msp_backfill``). Un fallback ``uuid_generate_v4()`` cubre el
caso ``uuid-ossp``.

Revision ID: y19msp24_refresh_session
Revises: x18msp23_device_fks
Create Date: 2026-09-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "y19msp24_refresh_session"
down_revision: Union[str, Sequence[str], None] = "x18msp23_device_fks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("session_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("session_started_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("ip_address", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("user_agent", sa.String(), nullable=True))

    # Backfill temporal antes de imponer NOT NULL. ``last_used_at`` y
    # ``session_started_at`` = ``created_at`` (la sesión arrancó ahí y no
    # sabemos si se movió después). ``session_id`` = UUID nuevo por fila.
    if is_sqlite:
        uuid_expr = "lower(hex(randomblob(4))) || '-' || lower(hex(randomblob(2))) || '-4' || substr(lower(hex(randomblob(2))),2) || '-' || substr('89ab',abs(random())%4+1,1) || substr(lower(hex(randomblob(2))),2) || '-' || lower(hex(randomblob(6)))"
    else:
        # Postgres — pgcrypto ya cargada por e2msp2_msp_backfill.
        uuid_expr = "gen_random_uuid()::text"

    op.execute(
        sa.text(
            f"UPDATE refresh_tokens "
            f"SET last_used_at = created_at, "
            f"    session_started_at = created_at, "
            f"    session_id = {uuid_expr} "
            f"WHERE last_used_at IS NULL"
        )
    )

    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.alter_column("last_used_at", nullable=False)
        batch_op.alter_column("session_id", nullable=False)
        batch_op.alter_column("session_started_at", nullable=False)
        batch_op.create_index(
            "ix_refresh_tokens_session_id", ["session_id"], unique=False
        )
        batch_op.create_index(
            "ix_refresh_tokens_parent_id", ["parent_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_refresh_tokens_parent_id",
            "refresh_tokens",
            ["parent_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("refresh_tokens", schema=None) as batch_op:
        batch_op.drop_constraint("fk_refresh_tokens_parent_id", type_="foreignkey")
        batch_op.drop_index("ix_refresh_tokens_parent_id")
        batch_op.drop_index("ix_refresh_tokens_session_id")
        batch_op.drop_column("user_agent")
        batch_op.drop_column("ip_address")
        batch_op.drop_column("parent_id")
        batch_op.drop_column("session_started_at")
        batch_op.drop_column("session_id")
        batch_op.drop_column("last_used_at")
