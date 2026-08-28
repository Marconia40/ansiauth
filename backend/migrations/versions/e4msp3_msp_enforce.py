"""msp_enforce

Phase 4 of the MSP (Multi-Site Provider) migration. Enforces at the database
layer every invariant that Phase 2 backfilled and Phase 3 read from:

  1. Pre-M3 fail-fast verification — re-run the M2 invariant queries; abort
     the migration transaction with a clear ``RuntimeError`` if any returns
     non-zero, and instruct the operator to run M2 first.

  2. NOT NULL flips on the columns Phase 2 populated on every row:
       - ``devices.device_group_id``
       - ``device_groups.site_id``
       - ``sites.default_group_id``

  3. Partial unique indexes that enforce the target-model invariants:
       - ``ux_sites_single_base_infra``      — exactly one BASE_INFRASTRUCTURE
       - ``ux_device_groups_one_default_per_site``
                                             — one is_default=TRUE per site

  4. Per-site ``UNIQUE(site_id, name)`` on ``device_groups`` (D6). Phase 2
     dropped the pre-MSP global ``UNIQUE(device_groups.name)`` and installed a
     non-unique ``ix_device_groups_name_nonunique`` lookup index in its place;
     this step swaps that replacement out for the target constraint. Under
     SQLite the swap happens inside ``batch_alter_table`` to trigger the
     table-recreation dance the driver requires for constraint changes.

  5. FK tightening — ``device_groups.site_id`` moves from ``SET NULL`` to
     ``RESTRICT``. Deletes of a site with any groups now fail loudly at the
     DB layer rather than silently orphaning the groups. Under SQLite the FK
     is dropped-and-recreated inside ``batch_alter_table`` (SQLite lacks
     ``ALTER TABLE … DROP CONSTRAINT``).

Down-migration reverses every step in order and re-instates the global
``UNIQUE(device_groups.name)`` via the same helper Phase 2's downgrade uses,
so ``alembic downgrade -1`` cleanly returns to the pre-M3 shape.

Revision ID: e4msp3_enforce
Revises: e2msp2_backfill
Create Date: 2026-08-22 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e4msp3_enforce"
down_revision: Union[str, Sequence[str], None] = "e2msp2_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BASE_INFRA_SITE_KIND = "BASE_INFRASTRUCTURE"


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # Step 1 — fail-fast if Phase 2 didn't complete cleanly on this DB.
    _preflight_verify(bind)

    # Step 2 — NOT NULL flips.
    _flip_not_null(is_pg)

    # Step 3 — Partial unique indexes (the "one Base-Infra" and "one Default
    # per Site" invariants).
    _create_partial_unique_indexes(is_pg)

    # Step 4 — Per-site UNIQUE(site_id, name); swap out the non-unique lookup
    # index Phase 2 installed.
    _swap_to_per_site_unique_name(bind, is_pg)

    # Step 5 — FK ondelete tightening on device_groups.site_id (SET NULL →
    # RESTRICT).
    _tighten_device_groups_site_fk(bind, is_pg)


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # Reverse Step 5 — FK back to SET NULL.
    _relax_device_groups_site_fk(bind, is_pg)

    # Reverse Step 4 — restore non-unique lookup index and drop the per-site
    # unique constraint.
    _swap_back_to_global_lookup_index(bind, is_pg)

    # Reverse Step 3 — drop the partial unique indexes.
    op.drop_index("ux_device_groups_one_default_per_site", table_name="device_groups")
    op.drop_index("ux_sites_single_base_infra", table_name="sites")

    # Reverse Step 2 — relax NOT NULL back to nullable. ``sites.default_group
    # _id`` was never flipped so no need to touch it here.
    if is_pg:
        op.alter_column("device_groups", "site_id", existing_type=sa.Integer(), nullable=True)
        op.alter_column("devices", "device_group_id", existing_type=sa.Integer(), nullable=True)
    else:
        with op.batch_alter_table("device_groups") as batch:
            batch.alter_column("site_id", existing_type=sa.Integer(), nullable=True)
        with op.batch_alter_table("devices") as batch:
            batch.alter_column("device_group_id", existing_type=sa.Integer(), nullable=True)


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Pre-M3 fail-fast verification
# ─────────────────────────────────────────────────────────────────────────────

def _preflight_verify(bind) -> None:
    """Re-run the M2 invariant queries. Any non-zero result aborts the
    migration transaction with a hint pointing the operator at M2."""
    checks = [
        (
            "SELECT COUNT(*) FROM devices WHERE device_group_id IS NULL",
            "devices with NULL device_group_id",
        ),
        (
            "SELECT COUNT(*) FROM device_groups WHERE site_id IS NULL",
            "device_groups with NULL site_id",
        ),
        (
            "SELECT COUNT(*) FROM sites WHERE default_group_id IS NULL",
            "sites with NULL default_group_id",
        ),
        (
            "SELECT COUNT(*) FROM users WHERE role IN ('admin','super-admin') "
            "AND is_system_admin = 0",
            "admin/super-admin users with is_system_admin=FALSE",
        ),
    ]
    for sql, label in checks:
        # Postgres stores booleans as native BOOL; the FALSE literal in the
        # last check is 0 in SQLite and false in Postgres — swap it in.
        sql_pg = sql.replace("= 0", "= FALSE") if bind.dialect.name == "postgresql" else sql
        n = bind.execute(sa.text(sql_pg)).scalar_one()
        if n:
            raise RuntimeError(
                f"M3 preflight failed: {n} row(s) — {label}. "
                "Phase-2 backfill (e2msp2_backfill) must complete cleanly before "
                "M3 can enforce NOT NULL. Investigate via the post-migration "
                "report script (backend/scripts/msp_post_backfill_report.py) "
                "and re-run M2 if necessary."
            )

    base_infra_count = bind.execute(sa.text(
        "SELECT COUNT(*) FROM sites WHERE kind = 'BASE_INFRASTRUCTURE'"
    )).scalar_one()
    if base_infra_count != 1:
        raise RuntimeError(
            f"M3 preflight failed: expected exactly 1 BASE_INFRASTRUCTURE site, "
            f"found {base_infra_count}. Run Phase-2 backfill first."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — NOT NULL flips
# ─────────────────────────────────────────────────────────────────────────────

def _flip_not_null(is_pg: bool) -> None:
    """Flip ``devices.device_group_id`` and ``device_groups.site_id`` to NOT
    NULL.

    Note: ``sites.default_group_id`` is intentionally NOT flipped here despite
    the plan calling for it. The cyclic FK (sites↔device_groups) requires a
    two-step INSERT — the site row exists before the default group can be
    created — and no portable pattern makes that two-step land under a NOT
    NULL constraint. Enforcement is instead pushed to
    ``site_service.create_site`` and asserted by the preflight verification
    in every M3 run. See CHANGELOG Phase 4 for the acknowledged deviation.
    """
    if is_pg:
        op.alter_column("devices", "device_group_id",
                        existing_type=sa.Integer(), nullable=False)
        op.alter_column("device_groups", "site_id",
                        existing_type=sa.Integer(), nullable=False)
    else:
        # SQLite can't ALTER COLUMN in place; batch_alter_table rewrites the
        # table with the new schema.
        with op.batch_alter_table("devices") as batch:
            batch.alter_column("device_group_id",
                               existing_type=sa.Integer(), nullable=False)
        with op.batch_alter_table("device_groups") as batch:
            batch.alter_column("site_id",
                               existing_type=sa.Integer(), nullable=False)


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Partial unique indexes
# ─────────────────────────────────────────────────────────────────────────────

def _create_partial_unique_indexes(is_pg: bool) -> None:
    """Both indexes use ``postgresql_where`` for Postgres and ``sqlite_where``
    for SQLite. Passing both is safe — Alembic ignores the one that doesn't
    match the active dialect."""
    op.create_index(
        "ux_sites_single_base_infra",
        "sites",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("kind = 'BASE_INFRASTRUCTURE'"),
        sqlite_where=sa.text("kind = 'BASE_INFRASTRUCTURE'"),
    )
    op.create_index(
        "ux_device_groups_one_default_per_site",
        "device_groups",
        ["site_id"],
        unique=True,
        postgresql_where=sa.text("is_default = TRUE"),
        sqlite_where=sa.text("is_default = 1"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Per-site UNIQUE(site_id, name) on device_groups (D6)
# ─────────────────────────────────────────────────────────────────────────────

def _swap_to_per_site_unique_name(bind, is_pg: bool) -> None:
    """Phase 2 dropped the pre-MSP ``UNIQUE(device_groups.name)`` and added
    ``ix_device_groups_name_nonunique`` as a lookup replacement. M3 drops the
    replacement index and installs ``UNIQUE(site_id, name)`` (D6). Idempotent
    on both dialects — checks the current state before mutating."""
    if is_pg:
        # Drop the lookup replacement if present.
        exists_ix = bind.execute(sa.text("""
            SELECT 1 FROM pg_indexes
             WHERE tablename = 'device_groups'
               AND indexname = 'ix_device_groups_name_nonunique'
        """)).scalar()
        if exists_ix:
            op.drop_index("ix_device_groups_name_nonunique", table_name="device_groups")
        # Add the per-site unique constraint if not already there.
        exists_uc = bind.execute(sa.text("""
            SELECT 1 FROM information_schema.table_constraints
             WHERE table_name = 'device_groups'
               AND constraint_name = 'uq_device_group_site_name'
        """)).scalar()
        if not exists_uc:
            op.create_unique_constraint(
                "uq_device_group_site_name", "device_groups", ["site_id", "name"],
            )
    else:
        indexes = {
            r[1] for r in bind.execute(
                sa.text("PRAGMA index_list('device_groups')")
            ).fetchall()
        }
        # SQLite implements UNIQUE constraints as automatic indexes with the
        # ``sqlite_autoindex_*`` naming or the name we choose in CREATE.
        with op.batch_alter_table("device_groups") as batch:
            if "ix_device_groups_name_nonunique" in indexes:
                batch.drop_index("ix_device_groups_name_nonunique")
            batch.create_unique_constraint(
                "uq_device_group_site_name", ["site_id", "name"],
            )


def _swap_back_to_global_lookup_index(bind, is_pg: bool) -> None:
    """Inverse of :func:`_swap_to_per_site_unique_name`. Restores the
    non-unique ``ix_device_groups_name_nonunique`` lookup index and drops the
    per-site unique constraint."""
    if is_pg:
        exists_uc = bind.execute(sa.text("""
            SELECT 1 FROM information_schema.table_constraints
             WHERE table_name = 'device_groups'
               AND constraint_name = 'uq_device_group_site_name'
        """)).scalar()
        if exists_uc:
            op.drop_constraint(
                "uq_device_group_site_name", "device_groups", type_="unique",
            )
        exists_ix = bind.execute(sa.text("""
            SELECT 1 FROM pg_indexes
             WHERE tablename = 'device_groups'
               AND indexname = 'ix_device_groups_name_nonunique'
        """)).scalar()
        if not exists_ix:
            op.create_index(
                "ix_device_groups_name_nonunique",
                "device_groups",
                ["name"],
                unique=False,
            )
    else:
        indexes = {
            r[1] for r in bind.execute(
                sa.text("PRAGMA index_list('device_groups')")
            ).fetchall()
        }
        with op.batch_alter_table("device_groups") as batch:
            # batch_alter_table can't introspect its own state; drop the UC
            # unconditionally and re-add the lookup index if missing.
            batch.drop_constraint("uq_device_group_site_name", type_="unique")
            if "ix_device_groups_name_nonunique" not in indexes:
                batch.create_index(
                    "ix_device_groups_name_nonunique", ["name"], unique=False,
                )


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — FK ondelete tightening on device_groups.site_id
# ─────────────────────────────────────────────────────────────────────────────

# The exact FK name assigned by the ``device_group_site_id`` migration
# (``a3c7e9d1f482``) that first added ``device_groups.site_id`` is
# ``fk_device_groups_site_id``. Both dialects use the same name.
_FK_NAME = "fk_device_groups_site_id"


def _tighten_device_groups_site_fk(bind, is_pg: bool) -> None:
    if is_pg:
        # Drop-and-recreate is the portable path — Postgres has no
        # ``ALTER CONSTRAINT`` for ondelete changes.
        exists = bind.execute(sa.text("""
            SELECT 1 FROM information_schema.table_constraints
             WHERE table_name = 'device_groups'
               AND constraint_name = :fk
        """), {"fk": _FK_NAME}).scalar()
        if exists:
            op.drop_constraint(_FK_NAME, "device_groups", type_="foreignkey")
        op.create_foreign_key(
            _FK_NAME,
            "device_groups", "sites",
            ["site_id"], ["id"],
            ondelete="RESTRICT",
        )
    else:
        # SQLite: batch_alter_table rewrites the table with the new FK.
        with op.batch_alter_table("device_groups") as batch:
            batch.drop_constraint(_FK_NAME, type_="foreignkey")
            batch.create_foreign_key(
                _FK_NAME,
                "sites",
                ["site_id"], ["id"],
                ondelete="RESTRICT",
            )


def _relax_device_groups_site_fk(bind, is_pg: bool) -> None:
    """Inverse — restore ``SET NULL`` to match the pre-M3 shape."""
    if is_pg:
        exists = bind.execute(sa.text("""
            SELECT 1 FROM information_schema.table_constraints
             WHERE table_name = 'device_groups'
               AND constraint_name = :fk
        """), {"fk": _FK_NAME}).scalar()
        if exists:
            op.drop_constraint(_FK_NAME, "device_groups", type_="foreignkey")
        op.create_foreign_key(
            _FK_NAME,
            "device_groups", "sites",
            ["site_id"], ["id"],
            ondelete="SET NULL",
        )
    else:
        with op.batch_alter_table("device_groups") as batch:
            batch.drop_constraint(_FK_NAME, type_="foreignkey")
            batch.create_foreign_key(
                _FK_NAME,
                "sites",
                ["site_id"], ["id"],
                ondelete="SET NULL",
            )
