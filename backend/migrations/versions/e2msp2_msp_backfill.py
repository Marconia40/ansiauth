"""msp_backfill

Phase 2 of the MSP (Multi-Site Provider) migration. Populates every column /
table that Phase 1 added on every existing row. Data-only — no schema
changes.

Six steps (mirror docs/upgrades/phases/phase-2-backfill.md §2):
  1. Base-Infrastructure site + its Default group (idempotent with Phase 1
     ``ensure_base_infrastructure()``).
  2. A Default group for every REGULAR site that lacks one.
  3. Assign every device to a device_group:
     3a. Site-less devices → Base Infra's Default.
     3b. Devices with exactly one group membership inside their site → that
         group.
     3c. Remainder (multi-group or zero-group inside their site) → the
         site's Default.
  4. Audit-log every device that Step 3c touched **and** was previously a
     member of >1 group in its site (R1 mitigation).
  5. Delete legacy device_groups whose site_id IS NULL (D21a) and their
     members. Runs after Step 3 so no live device still references them.
  6. User grant migration (D24 answer):
     6a. UPDATE users SET is_system_admin=TRUE WHERE role IN
         ('admin','super-admin').
     6b. INSERT site-scoped role_assignments for every operator/observer
         user × their allowed_sites row.
     6c. **Deliberately NOT executed** — D14 says Base Infrastructure is
         is_system_admin-only. Users who previously matched the site_id IS
         NULL fallback are captured by the post-migration report script
         (backend/scripts/msp_post_backfill_report.py) so the operator can
         decide per-user.

Idempotency: every statement is either INSERT WHERE NOT EXISTS or UPDATE
WHERE <target> IS NULL. Running twice = running once. Enforced by
test_msp_m2_idempotent.py.

Verification: five _assert_zero() calls at the end of upgrade(). Any
non-zero result raises RuntimeError and aborts the migration transaction.

See: docs/upgrades/phases/phase-2-backfill.md
See: docs/MSP_IMPLEMENTATION_PLAN.md §23 (M2), §24, D5, D14, D19, D24.

Revision ID: e2msp2_backfill
Revises: e1msp1_additive
Create Date: 2026-08-20 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2msp2_backfill"
down_revision: Union[str, Sequence[str], None] = "e1msp1_additive"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ─────────────────────────────────────────────────────────────────────────────
# Constants — must mirror ``app.services.site_service`` and Phase 1.
# ─────────────────────────────────────────────────────────────────────────────
BASE_INFRA_NAME = "Base Infrastructure"
BASE_INFRA_KIND = "BASE_INFRASTRUCTURE"
REGULAR_KIND = "REGULAR"
DEFAULT_GROUP_NAME = "Default"


def _assert_zero(bind, sql: str, msg: str) -> None:
    n = bind.execute(sa.text(sql)).scalar() or 0
    if n:
        raise RuntimeError(f"MSP M2 verification failed: {msg} (got {n})")


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # ────────────────────────────────────────────────────────────────────────
    # Step 0 — Drop the global UNIQUE constraint / index on device_groups.name.
    #
    # The initial device_groups migration (e7f4a2b9c810) declared
    # ``UniqueConstraint('name')`` + a UNIQUE index. That constraint predates
    # MSP; the MSP target model uses per-site unique ``(site_id, name)``
    # (Phase 4). Since Step 2 below inserts one ``'Default'`` group per
    # site, we must drop the global constraint here.
    #
    # This is a schema tweak inside a "data-only" phase — an acknowledged
    # deviation from the plan's phase-2 scope, but the alternative (a
    # separate one-line schema migration between M1 and M2) buys no
    # additional safety. Documented in the CHANGELOG Phase-2 entry.
    #
    # Idempotent: guarded on constraint / index existence so re-running
    # is safe.
    # ────────────────────────────────────────────────────────────────────────
    _drop_global_unique_on_device_groups_name(bind, is_pg)

    # ────────────────────────────────────────────────────────────────────────
    # Step 1 — Base-Infrastructure site + its Default group.
    #
    # Phase 1's ensure_base_infrastructure() may have already created these
    # rows at app startup. The NOT EXISTS / IS NULL clauses make this a
    # no-op in that case.
    # ────────────────────────────────────────────────────────────────────────
    op.execute(sa.text(f"""
        INSERT INTO sites (name, description, kind, created_at, updated_at)
        SELECT :name, :desc, :kind, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
         WHERE NOT EXISTS (
             SELECT 1 FROM sites WHERE kind = :kind
         )
    """).bindparams(
        name=BASE_INFRA_NAME,
        desc="System-managed base infrastructure site.",
        kind=BASE_INFRA_KIND,
    ))

    op.execute(sa.text(f"""
        INSERT INTO device_groups (name, description, site_id, is_default, created_at)
        SELECT :name, :desc, s.id, {_true(is_pg)}, CURRENT_TIMESTAMP
          FROM sites s
         WHERE s.kind = :kind
           AND s.default_group_id IS NULL
           AND NOT EXISTS (
               SELECT 1 FROM device_groups g
                WHERE g.site_id = s.id AND g.is_default = {_true(is_pg)}
           )
    """).bindparams(
        name=DEFAULT_GROUP_NAME,
        desc=f"Default group for {BASE_INFRA_NAME}.",
        kind=BASE_INFRA_KIND,
    ))

    op.execute(sa.text(f"""
        UPDATE sites
           SET default_group_id = (
               SELECT g.id FROM device_groups g
                WHERE g.site_id = sites.id AND g.is_default = {_true(is_pg)}
                LIMIT 1
           )
         WHERE default_group_id IS NULL
           AND kind = :kind
    """).bindparams(kind=BASE_INFRA_KIND))

    # ────────────────────────────────────────────────────────────────────────
    # Step 2 — A Default group for every REGULAR site that lacks one.
    #
    # D6 collision handling: if a user has already named a group 'Default'
    # inside a site whose default_group_id is still NULL, promote it rather
    # than colliding with the per-site unique (site_id, name) constraint
    # that Phase 4 will add.
    # ────────────────────────────────────────────────────────────────────────
    op.execute(sa.text(f"""
        UPDATE device_groups
           SET is_default = {_true(is_pg)}
         WHERE name = :name
           AND is_default = {_false(is_pg)}
           AND site_id IN (
               SELECT id FROM sites
                WHERE default_group_id IS NULL AND kind = :kind
           )
    """).bindparams(name=DEFAULT_GROUP_NAME, kind=REGULAR_KIND))

    op.execute(sa.text(f"""
        INSERT INTO device_groups (name, description, site_id, is_default, created_at)
        SELECT :name, 'Default group for site ' || s.name, s.id, {_true(is_pg)}, CURRENT_TIMESTAMP
          FROM sites s
         WHERE s.kind = :kind
           AND s.default_group_id IS NULL
           AND NOT EXISTS (
               SELECT 1 FROM device_groups g
                WHERE g.site_id = s.id AND g.is_default = {_true(is_pg)}
           )
    """).bindparams(name=DEFAULT_GROUP_NAME, kind=REGULAR_KIND))

    op.execute(sa.text(f"""
        UPDATE sites
           SET default_group_id = (
               SELECT g.id FROM device_groups g
                WHERE g.site_id = sites.id AND g.is_default = {_true(is_pg)}
                LIMIT 1
           )
         WHERE default_group_id IS NULL
           AND kind = :kind
    """).bindparams(kind=REGULAR_KIND))

    # ────────────────────────────────────────────────────────────────────────
    # Step 3 — Assign every device to a device_group.
    # ────────────────────────────────────────────────────────────────────────

    # 3a. Site-less devices → Base Infra's Default.
    op.execute(sa.text("""
        UPDATE devices
           SET device_group_id = (
               SELECT default_group_id FROM sites
                WHERE kind = :kind
                LIMIT 1
           )
         WHERE device_group_id IS NULL
           AND site_id IS NULL
    """).bindparams(kind=BASE_INFRA_KIND))

    # 3b. Devices with exactly one group inside their site → that group.
    #     Written as scalar subqueries so it is portable across SQLite and
    #     Postgres without relying on ``UPDATE ... FROM`` alias support.
    op.execute(sa.text("""
        UPDATE devices
           SET device_group_id = (
               SELECT MIN(m.group_id)
                 FROM device_group_members m
                 JOIN device_groups g ON g.id = m.group_id
                WHERE m.device_name = devices.name
                  AND g.site_id = devices.site_id
           )
         WHERE device_group_id IS NULL
           AND site_id IS NOT NULL
           AND (
               SELECT COUNT(DISTINCT m.group_id)
                 FROM device_group_members m
                 JOIN device_groups g ON g.id = m.group_id
                WHERE m.device_name = devices.name
                  AND g.site_id = devices.site_id
           ) = 1
    """))

    # 3c. Remainder (ambiguous multi-group or zero-group inside their site)
    #     → the site's Default group.
    op.execute(sa.text("""
        UPDATE devices
           SET device_group_id = (
               SELECT s.default_group_id FROM sites s WHERE s.id = devices.site_id
           )
         WHERE device_group_id IS NULL
           AND site_id IS NOT NULL
    """))

    # ────────────────────────────────────────────────────────────────────────
    # Step 4 — Audit-log ambiguous multi-group backfill assignments.
    #
    # A row for every device that was previously a member of >1 group inside
    # its site. Emits a single INSERT ... SELECT with GROUP BY device.
    # Dialect-branched because SQLite lacks json_build_object / array_agg.
    # ────────────────────────────────────────────────────────────────────────
    if is_pg:
        op.execute(sa.text("""
            INSERT INTO audit_logs (timestamp, "user", action, resource, resource_id,
                                    status, details)
            SELECT CURRENT_TIMESTAMP,
                   'msp_migration',
                   'msp_migration_ambiguous_group_assignment',
                   'device',
                   d.name,
                   'success',
                   json_build_object(
                       'previous_groups', array_agg(DISTINCT dgm.group_id ORDER BY dgm.group_id),
                       'assigned_to', d.device_group_id
                   )
              FROM devices d
              JOIN device_group_members dgm ON dgm.device_name = d.name
              JOIN device_groups g ON g.id = dgm.group_id
             WHERE d.site_id = g.site_id
               AND NOT EXISTS (
                   SELECT 1 FROM audit_logs al
                    WHERE al.action = 'msp_migration_ambiguous_group_assignment'
                      AND al.resource_id = d.name
               )
             GROUP BY d.name, d.device_group_id
            HAVING COUNT(DISTINCT dgm.group_id) > 1
        """))
    else:
        # SQLite — build the JSON string by hand with group_concat.
        op.execute(sa.text("""
            INSERT INTO audit_logs (timestamp, "user", action, resource, resource_id,
                                    status, details)
            SELECT CURRENT_TIMESTAMP,
                   'msp_migration',
                   'msp_migration_ambiguous_group_assignment',
                   'device',
                   d.name,
                   'success',
                   '{"previous_groups": [' ||
                       group_concat(DISTINCT dgm.group_id) ||
                   '], "assigned_to": ' ||
                       CAST(d.device_group_id AS TEXT) ||
                   '}'
              FROM devices d
              JOIN device_group_members dgm ON dgm.device_name = d.name
              JOIN device_groups g ON g.id = dgm.group_id
             WHERE d.site_id = g.site_id
               AND NOT EXISTS (
                   SELECT 1 FROM audit_logs al
                    WHERE al.action = 'msp_migration_ambiguous_group_assignment'
                      AND al.resource_id = d.name
               )
             GROUP BY d.name, d.device_group_id
            HAVING COUNT(DISTINCT dgm.group_id) > 1
        """))

    # ────────────────────────────────────────────────────────────────────────
    # Step 5 — Delete legacy device_groups whose site_id IS NULL (D21a).
    # Runs *after* Step 3 so no device_group_id still references them.
    # ────────────────────────────────────────────────────────────────────────
    op.execute(sa.text("""
        DELETE FROM device_group_members
         WHERE group_id IN (
             SELECT id FROM device_groups WHERE site_id IS NULL
         )
    """))
    op.execute(sa.text("DELETE FROM device_groups WHERE site_id IS NULL"))

    # ────────────────────────────────────────────────────────────────────────
    # Step 6 — User grant migration.
    #
    # 6a. Promote admin/super-admin to is_system_admin.
    # 6b. Site-scoped role_assignments for every (operator|observer, allowed_site).
    # 6c. NOT executed (D14: Base Infra is is_system_admin-only; the
    #     post-migration report lists users who lose Base-Infra visibility
    #     so the operator can decide per-user).
    # ────────────────────────────────────────────────────────────────────────
    op.execute(sa.text(f"""
        UPDATE users
           SET is_system_admin = {_true(is_pg)}
         WHERE role IN ('admin', 'super-admin')
           AND is_system_admin = {_false(is_pg)}
    """))

    op.execute(sa.text("""
        INSERT INTO role_assignments
               (user_id, site_id, device_group_id, role, created_at, created_by_user_id)
        SELECT u.id, uas.site_id, NULL, u.role, CURRENT_TIMESTAMP, NULL
          FROM users u
          JOIN user_allowed_sites uas ON uas.user_id = u.id
          JOIN sites s ON s.id = uas.site_id
         WHERE u.role IN ('operator', 'observer')
           AND s.kind = :kind
           AND NOT EXISTS (
               SELECT 1 FROM role_assignments ra
                WHERE ra.user_id = u.id
                  AND ra.site_id = uas.site_id
                  AND ra.device_group_id IS NULL
           )
    """).bindparams(kind=REGULAR_KIND))

    # ────────────────────────────────────────────────────────────────────────
    # Verification — fail loudly if any invariant is violated.
    # ────────────────────────────────────────────────────────────────────────
    _assert_zero(
        bind,
        "SELECT COUNT(*) FROM devices WHERE device_group_id IS NULL",
        "devices with no device_group_id",
    )
    _assert_zero(
        bind,
        "SELECT COUNT(*) FROM device_groups WHERE site_id IS NULL",
        "device_groups with no site_id",
    )
    _assert_zero(
        bind,
        "SELECT COUNT(*) FROM sites WHERE default_group_id IS NULL",
        "sites with no default_group_id",
    )
    _assert_zero(
        bind,
        "SELECT COUNT(*) FROM users "
        "WHERE role IN ('admin', 'super-admin') AND is_system_admin = "
        + _false(is_pg),
        "admin/super-admin users not marked is_system_admin",
    )

    base_infra_count = bind.execute(sa.text(
        "SELECT COUNT(*) FROM sites WHERE kind = :kind"
    ).bindparams(kind=BASE_INFRA_KIND)).scalar() or 0
    if base_infra_count != 1:
        raise RuntimeError(
            f"MSP M2 verification failed: expected exactly 1 Base-Infrastructure "
            f"site, got {base_infra_count}"
        )


def downgrade() -> None:
    """Clear the columns / rows this migration populated.

    Not intended for production recovery — use the T0.2 backup instead. Step
    5 (deletion of legacy null-site groups) is not reversible; those rows
    stay gone. Step 4's audit_logs rows are removed via a targeted DELETE.
    The application-layer AuditImmutabilityError only fires on ORM UPDATEs,
    so raw-SQL DELETE succeeds.
    """
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    op.execute(sa.text(
        "DELETE FROM audit_logs WHERE action LIKE 'msp_migration_%'"
    ))
    op.execute(sa.text("DELETE FROM role_assignments"))
    op.execute(sa.text("UPDATE devices SET device_group_id = NULL"))
    op.execute(sa.text("UPDATE sites SET default_group_id = NULL"))
    op.execute(sa.text(
        f"UPDATE users SET is_system_admin = {_false(is_pg)}"
    ))
    # Delete every default group this migration created. A user-created group
    # that Step 2 promoted to is_default=TRUE is *not* touched — dropping it
    # would be data loss beyond what the migration itself introduced.
    op.execute(sa.text(f"""
        DELETE FROM device_groups
         WHERE is_default = {_true(is_pg)}
           AND description LIKE 'Default group for%'
    """))
    op.execute(sa.text(
        "DELETE FROM sites WHERE kind = :kind"
    ).bindparams(kind=BASE_INFRA_KIND))

    # Reinstate Step 0's dropped unique constraint / index. Only safe once
    # the DELETEs above have removed the M2-inserted duplicates; if a
    # user-created row now conflicts (extremely unlikely), the operator
    # must resolve it before rollback completes.
    _restore_global_unique_on_device_groups_name(bind, is_pg)


# ─────────────────────────────────────────────────────────────────────────────
# Dialect-portable boolean literals. SQLite accepts 0/1; Postgres accepts
# TRUE/FALSE. Using the correct literal per-dialect keeps the same SQL
# strings portable across both.
# ─────────────────────────────────────────────────────────────────────────────
def _true(is_pg: bool) -> str:
    return "TRUE" if is_pg else "1"


def _false(is_pg: bool) -> str:
    return "FALSE" if is_pg else "0"


def _drop_global_unique_on_device_groups_name(bind, is_pg: bool) -> None:
    """Drop the pre-MSP global ``UNIQUE(device_groups.name)`` (constraint +
    unique index). Idempotent on both dialects.

    Postgres: check ``information_schema`` before dropping to keep re-runs
    safe. SQLite: use ``batch_alter_table`` which recreates the table
    without the constraint; guard with a table-info scan.
    """
    if is_pg:
        exists = bind.execute(sa.text("""
            SELECT 1 FROM information_schema.table_constraints
             WHERE table_name = 'device_groups'
               AND constraint_name = 'uq_device_group_name'
        """)).scalar()
        if exists:
            op.drop_constraint(
                "uq_device_group_name", "device_groups", type_="unique"
            )
        # Drop the accompanying unique index if present.
        exists_ix = bind.execute(sa.text("""
            SELECT 1 FROM pg_indexes
             WHERE tablename = 'device_groups'
               AND indexname = 'ix_device_groups_name'
        """)).scalar()
        if exists_ix:
            op.drop_index("ix_device_groups_name", table_name="device_groups")
    else:
        # SQLite — two things enforce uniqueness on ``name``:
        #   1. ``UniqueConstraint('name', name='uq_device_group_name')``
        #   2. ``create_index('ix_device_groups_name', ['name'], unique=True)``
        # batch_alter_table recreates the table without the constraint; we
        # drop the unique index in the same batch and add a non-unique
        # replacement so ``ORDER BY name`` / ``WHERE name = ?`` stays fast.
        indexes = {
            r[1]
            for r in bind.execute(
                sa.text("PRAGMA index_list('device_groups')")
            ).fetchall()
        }
        if "ix_device_groups_name" in indexes:
            with op.batch_alter_table("device_groups") as batch_op:
                batch_op.drop_constraint(
                    "uq_device_group_name", type_="unique"
                )
                batch_op.drop_index("ix_device_groups_name")
                batch_op.create_index(
                    "ix_device_groups_name_nonunique", ["name"], unique=False
                )


def _restore_global_unique_on_device_groups_name(bind, is_pg: bool) -> None:
    """Inverse of :func:`_drop_global_unique_on_device_groups_name`. Used by
    ``downgrade()``. Requires that duplicate ``name`` rows have already
    been cleaned up by the downgrade's DELETE block above."""
    if is_pg:
        exists = bind.execute(sa.text("""
            SELECT 1 FROM information_schema.table_constraints
             WHERE table_name = 'device_groups'
               AND constraint_name = 'uq_device_group_name'
        """)).scalar()
        if not exists:
            op.create_unique_constraint(
                "uq_device_group_name", "device_groups", ["name"]
            )
        exists_ix = bind.execute(sa.text("""
            SELECT 1 FROM pg_indexes
             WHERE tablename = 'device_groups'
               AND indexname = 'ix_device_groups_name'
        """)).scalar()
        if not exists_ix:
            op.create_index(
                "ix_device_groups_name", "device_groups", ["name"], unique=True
            )
    else:
        indexes = {
            r[1]
            for r in bind.execute(
                sa.text("PRAGMA index_list('device_groups')")
            ).fetchall()
        }
        if "ix_device_groups_name" not in indexes:
            with op.batch_alter_table("device_groups") as batch_op:
                if "ix_device_groups_name_nonunique" in indexes:
                    batch_op.drop_index("ix_device_groups_name_nonunique")
                batch_op.create_unique_constraint(
                    "uq_device_group_name", ["name"]
                )
                batch_op.create_index(
                    "ix_device_groups_name", ["name"], unique=True
                )
