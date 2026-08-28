"""msp_rls

Phase 6 (M5) of the MSP (Multi-Site Provider) migration. Enables Postgres
Row-Level Security as **defense-in-depth** on top of the app-layer
``require_scope`` gate that Phases 3–4 established (decision D12).

The app connects to Postgres as ``ansiauth``, which owns the tables. Postgres
exempts table owners from RLS unless you explicitly ``FORCE ROW LEVEL
SECURITY``, so this migration enables + forces RLS on every scoped table.
Bootstrap and background code (which run without a JWT) go through
``app.core.rls_context.system_context()`` to satisfy the policies.

Read policies scope by the two Postgres GUCs the request middleware sets:

  * ``app.user_id``          — integer PK of the calling user (``0`` = deny)
  * ``app.is_system_admin``  — boolean; true bypasses every scope check

Read predicates mirror the app-layer scope resolution:

  * ``sites``          — system-admin, OR a grant on the site. Base
    Infrastructure is hidden from non-system-admins even with a stray
    grant (D14).
  * ``device_groups``  — system-admin, OR a grant on the group's site
    (site-wide) or on the group itself (group-scoped).
  * ``devices``        — same rule, resolved via the device's group's site.
  * ``audit_logs``     — system-admin, OR the row is about a resource the
    caller can see, OR it is an auth event (login/refresh — always visible
    to the caller themselves), OR it is a self-user event.

Write policies are **permissive** — the app layer's ``require_scope``
already gates writes at the boundary, and duplicating that check in RLS
risks silent inconsistencies (an INSERT that succeeds at the app but is
rejected by RLS looks like a bug). Read-side RLS is enough to make
ID-guessing impossible even under a raw-SQL bug (Phase 6 §2.2 rationale).

SQLite path: no-op. RLS is Postgres-only; dev environments and the pytest
suite keep working unchanged.

Revision ID: f6msp5_rls
Revises: e5msp4_cleanup
Create Date: 2026-08-22 20:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f6msp5_rls"
down_revision: Union[str, Sequence[str], None] = "e5msp4_cleanup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SCOPED_TABLES = ("sites", "device_groups", "devices", "audit_logs")


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite has no RLS — dev and CI stay functional without policies.
        return

    for table in _SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE so the ansiauth owner is also subject to policies — without
        # this, RLS is documentation-only in this deployment.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    _install_read_policies()
    _install_write_policies()
    # Drop SUPERUSER / BYPASSRLS from the app role LAST so if any earlier
    # DDL fails the migration can be retried without a permission downgrade
    # in the middle. SUPERUSER and BYPASSRLS both cause RLS to be silently
    # skipped; the docker-compose default image creates the app user as
    # SUPERUSER, which makes every policy above a no-op.
    _demote_app_role(bind)


def _demote_app_role(bind) -> None:
    """Remove SUPERUSER and BYPASSRLS from the role that owns this connection.

    Idempotent — the ``ALTER ROLE`` runs unconditionally but Postgres accepts
    the same attribute setting repeatedly. The change takes effect for new
    sessions; the current migration session keeps its existing attributes
    until it disconnects, which is safe because the demotion is the last
    step in ``upgrade()``.

    Retains ownership of the tables — ownership is separate from role
    attributes and is what lets the app continue to run migrations, create
    policies, and issue arbitrary DDL going forward.

    **Bootstrap-user caveat.** Postgres forbids demoting the *bootstrap
    user* — the role that initialised the database cluster — and returns
    ``permission denied to alter role`` if you try. In the docker-compose
    default setup ``POSTGRES_USER=ansiauth`` makes ``ansiauth`` the bootstrap
    user; that leaves the app connecting as an undemotable SUPERUSER, which
    bypasses every RLS policy this migration installed. Detection uses
    ``pg_roles.oid = 10`` (the fixed bootstrap-user oid) — if we're it, we
    log a warning and skip; the policies still exist for the day the app
    starts connecting as a non-super role (recommended for prod — see the
    Phase 6 deployment notes).
    """
    role = bind.execute(sa.text("SELECT current_user")).scalar_one()
    # Ops-only superuser roles that we should never touch.
    if role in ("postgres", "rds_superuser", "cloudsqlsuperuser"):
        return
    bootstrap_role = bind.execute(
        sa.text("SELECT rolname FROM pg_roles WHERE oid = 10")
    ).scalar()
    if role == bootstrap_role:
        import logging
        logging.getLogger("alembic.migration").warning(
            "MSP Phase 6: role '%s' is the Postgres bootstrap user and "
            "cannot be demoted. RLS policies installed but NOT enforced — "
            "the connecting SUPERUSER bypasses them silently. To enable "
            "RLS enforcement, connect the app as a separate non-SUPERUSER "
            "role that only owns runtime privileges on the schema. See "
            "docs/upgrades/phases/phase-6-rls-and-optional.md.",
            role,
        )
        return
    op.execute(f'ALTER ROLE "{role}" NOSUPERUSER NOBYPASSRLS')


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Restore SUPERUSER + BYPASSRLS on the app role FIRST — the current
    # migration session already has its attributes latched from connect
    # time, but any subsequent alembic step (or rerun) needs the elevated
    # role to succeed on tables it doesn't own. Same session-user guards
    # as upgrade: skip ops-only supers and skip the bootstrap user (which
    # was never demoted so needs no re-promotion).
    role = bind.execute(sa.text("SELECT current_user")).scalar_one()
    bootstrap_role = bind.execute(
        sa.text("SELECT rolname FROM pg_roles WHERE oid = 10")
    ).scalar()
    if role not in ("postgres", "rds_superuser", "cloudsqlsuperuser") \
            and role != bootstrap_role:
        op.execute(f'ALTER ROLE "{role}" SUPERUSER BYPASSRLS')

    # Drop policies, then disable RLS. Order matters only for cleanliness —
    # DISABLE RLS is legal with policies still attached.
    for table, policy in _all_policies():
        op.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")

    for table in reversed(_SCOPED_TABLES):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


# ─────────────────────────────────────────────────────────────────────────────
# Policy definitions
# ─────────────────────────────────────────────────────────────────────────────


def _all_policies():
    """Every ``(table, policy_name)`` pair created by :func:`upgrade`."""
    pairs = []
    for table in _SCOPED_TABLES:
        pairs.append((table, f"{table}_read"))
        pairs.append((table, f"{table}_insert"))
        pairs.append((table, f"{table}_update"))
        pairs.append((table, f"{table}_delete"))
    return pairs


def _install_read_policies() -> None:
    # sites — system-admin OR grant on the site (Base Infra hidden per D14).
    op.execute("""
        CREATE POLICY sites_read ON sites FOR SELECT USING (
            current_setting('app.is_system_admin', TRUE)::bool
            OR (
                kind <> 'BASE_INFRASTRUCTURE'
                AND EXISTS (
                    SELECT 1 FROM role_assignments ra
                     WHERE ra.user_id = current_setting('app.user_id', TRUE)::int
                       AND ra.site_id = sites.id
                )
            )
        )
    """)

    # device_groups — system-admin OR a matching site-wide/group-specific grant.
    op.execute("""
        CREATE POLICY device_groups_read ON device_groups FOR SELECT USING (
            current_setting('app.is_system_admin', TRUE)::bool
            OR EXISTS (
                SELECT 1 FROM role_assignments ra
                 WHERE ra.user_id = current_setting('app.user_id', TRUE)::int
                   AND ra.site_id = device_groups.site_id
                   AND (ra.device_group_id IS NULL
                        OR ra.device_group_id = device_groups.id)
            )
        )
    """)

    # devices — resolve site via the device's group.
    op.execute("""
        CREATE POLICY devices_read ON devices FOR SELECT USING (
            current_setting('app.is_system_admin', TRUE)::bool
            OR EXISTS (
                SELECT 1
                  FROM device_groups g
                  JOIN role_assignments ra
                    ON ra.site_id = g.site_id
                   AND (ra.device_group_id IS NULL
                        OR ra.device_group_id = g.id)
                 WHERE g.id = devices.device_group_id
                   AND ra.user_id = current_setting('app.user_id', TRUE)::int
            )
        )
    """)

    # audit_logs — five branches per phase-6-rls-and-optional.md §2.2:
    #   * system-admin bypass
    #   * device row visible → device audit rows visible
    #   * device_group row visible → group audit rows visible
    #   * site row visible → site audit rows visible
    #   * auth events always visible to the caller themselves
    #   * self-user events (login/refresh/etc.) visible to the caller
    # No blanket "device IS NULL" leak — every non-scoped row still needs a
    # matching user identity check.
    op.execute("""
        CREATE POLICY audit_logs_read ON audit_logs FOR SELECT USING (
            current_setting('app.is_system_admin', TRUE)::bool
            OR (
                resource = 'device' AND EXISTS (
                    SELECT 1 FROM devices d
                      JOIN device_groups g ON g.id = d.device_group_id
                      JOIN role_assignments ra
                        ON ra.site_id = g.site_id
                       AND (ra.device_group_id IS NULL OR ra.device_group_id = g.id)
                     WHERE d.name = audit_logs.resource_id
                       AND ra.user_id = current_setting('app.user_id', TRUE)::int
                )
            )
            OR (
                resource = 'device_group'
                AND audit_logs.resource_id ~ '^[0-9]+$'
                AND EXISTS (
                    SELECT 1 FROM device_groups g
                      JOIN role_assignments ra
                        ON ra.site_id = g.site_id
                       AND (ra.device_group_id IS NULL OR ra.device_group_id = g.id)
                     WHERE g.id = audit_logs.resource_id::int
                       AND ra.user_id = current_setting('app.user_id', TRUE)::int
                )
            )
            OR (
                resource = 'site'
                AND audit_logs.resource_id ~ '^[0-9]+$'
                AND EXISTS (
                    SELECT 1 FROM role_assignments ra
                     WHERE ra.site_id = audit_logs.resource_id::int
                       AND ra.user_id = current_setting('app.user_id', TRUE)::int
                )
            )
            OR (
                resource = 'user'
                AND EXISTS (
                    SELECT 1 FROM users u
                     WHERE u.id = current_setting('app.user_id', TRUE)::int
                       AND u.username = audit_logs."user"
                )
            )
            OR (
                resource = 'auth'
                AND EXISTS (
                    SELECT 1 FROM users u
                     WHERE u.id = current_setting('app.user_id', TRUE)::int
                       AND u.username = audit_logs."user"
                )
            )
        )
    """)


def _install_write_policies() -> None:
    """Permissive INSERT/UPDATE/DELETE policies — one per operation.

    RLS in Postgres denies operations that have no matching policy, so with
    RLS enabled we *must* provide policies for INSERT / UPDATE / DELETE or
    the app cannot mutate the tables at all. The app layer's
    ``require_scope`` already gates writes; RLS's role is defense-in-depth
    for reads only (see phase-6-rls-and-optional.md §2.2 rationale). ``true``
    passes every row through.

    Note — one policy *per operation*, not a single ``FOR ALL``. Multiple
    permissive policies on the same operation OR-combine; a ``FOR ALL``
    permissive policy with ``USING (true)`` would OR with the scoped SELECT
    policy and make every read wide open. Splitting by operation keeps the
    SELECT branch scoped and the write branches permissive.
    """
    for table in _SCOPED_TABLES:
        op.execute(f"""
            CREATE POLICY {table}_insert ON {table}
                FOR INSERT WITH CHECK (true)
        """)
        op.execute(f"""
            CREATE POLICY {table}_update ON {table}
                FOR UPDATE USING (true) WITH CHECK (true)
        """)
        op.execute(f"""
            CREATE POLICY {table}_delete ON {table}
                FOR DELETE USING (true)
        """)
