"""msp_cleanup

Phase 5 (M4) of the MSP (Multi-Site Provider) migration. Removes every legacy
column, table, and constraint kept for backward compatibility during Phases 3
and 4:

  1. Drop ``devices.site_id`` — the legacy denormalised site pointer. Devices
     now derive their site through ``device_group.site_id`` (Phase 1 added the
     direct FK; Phase 2 backfilled it; Phase 4 flipped it NOT NULL).

  2. Drop ``device_group_members`` — the M2M association replaced by the
     direct ``devices.device_group_id`` FK.

  3. Drop ``user_allowed_sites`` — the site-scoping M2M replaced by row-level
     ``role_assignments`` grants.

  4. Drop ``users.role`` — the single global privilege string replaced by
     ``users.is_system_admin`` (system-wide) and ``role_assignments``
     (per-scope).

WARNING — one-way migration in practice. ``downgrade()`` recreates the empty
tables/columns so the Alembic chain remains formally reversible, but it
CANNOT restore the dropped data. Recovery from a bad Phase-5 deploy requires
the pre-M4 database snapshot documented in
``docs/upgrades/phases/artifacts/phase0-db-snapshot-runbook.md`` and the
rollback runbook in ``docs/upgrades/phases/phase-5-cleanup.md`` §8. The
``downgrade()`` emits a NOTICE to that effect.

Revision ID: e5msp4_cleanup
Revises: e4msp3_enforce
Create Date: 2026-08-22 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5msp4_cleanup"
down_revision: Union[str, Sequence[str], None] = "e4msp3_enforce"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Exact names assigned by the original migrations that introduced each object.
# devices.site_id was added by 8b5d2c7a3e1f_add_sites; the FK is
# ``fk_devices_site_id`` (NOT ``fk_devices_site_id_sites``).
_DEVICES_SITE_ID_INDEX = "ix_devices_site_id"
_DEVICES_SITE_ID_FK = "fk_devices_site_id"


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # 1. devices.site_id — drop the index, FK, and column.
    _drop_devices_site_id(bind, is_pg)

    # 2. device_group_members — drop the M2M table.
    op.drop_table("device_group_members")

    # 3. user_allowed_sites — drop the M2M table.
    op.drop_table("user_allowed_sites")

    # 4. users.role — drop the legacy privilege column.
    _drop_users_role(is_pg)


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # One-way in practice — surface the fact loudly so an operator running
    # ``alembic downgrade`` after a botched Phase-5 deploy sees it in the
    # command output.
    op.execute(
        "SELECT 'M4 downgrade cannot restore dropped data — restore from the "
        "pre-Phase-5 snapshot instead (see phase-5-cleanup.md sec 8).'"
    )

    # Reverse in inverse order so FKs come back before the tables that
    # reference them do (users → device_group_members → user_allowed_sites
    # → devices.site_id).

    # 4. Recreate users.role (empty default 'observer' to satisfy NOT NULL).
    _recreate_users_role(is_pg)

    # 3. Recreate user_allowed_sites (empty).
    _recreate_user_allowed_sites()

    # 2. Recreate device_group_members (empty).
    _recreate_device_group_members()

    # 1. Recreate devices.site_id with the pre-M4 shape.
    _recreate_devices_site_id(bind, is_pg)


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — devices.site_id
# ─────────────────────────────────────────────────────────────────────────────


def _drop_devices_site_id(bind, is_pg: bool) -> None:
    if is_pg:
        # Postgres path — introspect and drop what's present. The index/FK are
        # both created unconditionally by 8b5d2c7a3e1f_add_sites so under a
        # clean chain they'll always exist; the guards are defensive against
        # partially-applied states.
        idx_exists = bind.execute(sa.text("""
            SELECT 1 FROM pg_indexes
             WHERE tablename = 'devices'
               AND indexname = :name
        """), {"name": _DEVICES_SITE_ID_INDEX}).scalar()
        if idx_exists:
            op.drop_index(_DEVICES_SITE_ID_INDEX, table_name="devices")

        fk_exists = bind.execute(sa.text("""
            SELECT 1 FROM information_schema.table_constraints
             WHERE table_name = 'devices'
               AND constraint_name = :name
        """), {"name": _DEVICES_SITE_ID_FK}).scalar()
        if fk_exists:
            op.drop_constraint(_DEVICES_SITE_ID_FK, "devices", type_="foreignkey")

        op.drop_column("devices", "site_id")
    else:
        # SQLite path — batch_alter_table rewrites the table without the
        # column. It drops in-batch indexes/constraints automatically as part
        # of the copy, but we call the drops explicitly to mirror the
        # 8b5d2c7a3e1f_add_sites down-migration and keep intent obvious.
        with op.batch_alter_table("devices") as batch:
            batch.drop_index(_DEVICES_SITE_ID_INDEX)
            batch.drop_constraint(_DEVICES_SITE_ID_FK, type_="foreignkey")
            batch.drop_column("site_id")


def _recreate_devices_site_id(bind, is_pg: bool) -> None:
    """Empty-column restore only — no data. See module docstring."""
    if is_pg:
        op.add_column(
            "devices",
            sa.Column("site_id", sa.Integer(), nullable=True),
        )
        op.create_foreign_key(
            _DEVICES_SITE_ID_FK,
            "devices", "sites",
            ["site_id"], ["id"],
            ondelete="SET NULL",
        )
        op.create_index(_DEVICES_SITE_ID_INDEX, "devices", ["site_id"], unique=False)
    else:
        with op.batch_alter_table("devices") as batch:
            batch.add_column(sa.Column("site_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                _DEVICES_SITE_ID_FK, "sites", ["site_id"], ["id"],
                ondelete="SET NULL",
            )
            batch.create_index(_DEVICES_SITE_ID_INDEX, ["site_id"], unique=False)


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — device_group_members
# ─────────────────────────────────────────────────────────────────────────────


def _recreate_device_group_members() -> None:
    """Mirrors the shape from e7f4a2b9c810_add_device_groups.upgrade — empty."""
    op.create_table(
        "device_group_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("device_name", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_name"], ["devices.name"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["device_groups.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "device_name", name="uq_group_member"),
    )
    with op.batch_alter_table("device_group_members") as batch:
        batch.create_index(
            "ix_device_group_members_group_id", ["group_id"], unique=False,
        )
        batch.create_index(
            "ix_device_group_members_device_name", ["device_name"], unique=False,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — user_allowed_sites
# ─────────────────────────────────────────────────────────────────────────────


def _recreate_user_allowed_sites() -> None:
    """Mirrors the shape from 9e2a4c8b6f10_add_user_allowed_sites.upgrade —
    empty (no backfill)."""
    op.create_table(
        "user_allowed_sites",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("site_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "site_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="CASCADE"),
    )
    with op.batch_alter_table("user_allowed_sites") as batch:
        batch.create_index(
            "ix_user_allowed_sites_user_id", ["user_id"], unique=False,
        )
        batch.create_index(
            "ix_user_allowed_sites_site_id", ["site_id"], unique=False,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — users.role
# ─────────────────────────────────────────────────────────────────────────────


def _drop_users_role(is_pg: bool) -> None:
    if is_pg:
        op.drop_column("users", "role")
    else:
        with op.batch_alter_table("users") as batch:
            batch.drop_column("role")


def _recreate_users_role(is_pg: bool) -> None:
    """Restore the column with a NOT NULL default of ``'observer'`` so any
    rows inserted after the downgrade satisfy the original constraint. Data
    for existing rows is NOT restored — see module docstring."""
    default_role = sa.text("'observer'")
    if is_pg:
        op.add_column(
            "users",
            sa.Column(
                "role", sa.String(), nullable=False, server_default=default_role,
            ),
        )
    else:
        with op.batch_alter_table("users") as batch:
            batch.add_column(
                sa.Column(
                    "role", sa.String(), nullable=False,
                    server_default=default_role,
                ),
            )
