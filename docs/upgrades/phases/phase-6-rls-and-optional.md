# Phase 6 — Postgres RLS + Optional Hardening

> **Goal.** Add Postgres Row-Level Security as defense-in-depth (confirmed required by **D12**), and land the optional Device-uniqueness-key enhancement.
> **Preconditions:** Phase 5 completed and soaked for at least one release cycle.
> **Design principles:** see [phase-0-prerequisites.md §1](phase-0-prerequisites.md#1-cross-phase-design-principles-apply-to-every-phase).
> **Companion plan sections:** §11.7, §14, §27, §32 Phase 6 tasks, decisions D12, D15.
> **Status change from plan:** the original plan marked this phase optional; the user's answer to D12 makes RLS a first-class requirement.

---

## 1. Scope summary

| Layer | Change | Notes |
|---|---|---|
| Alembic | 1 new revision `msp_rls` — enable RLS + policies | Postgres only; SQLite skips |
| Middleware | Set `app.user_id` / `app.is_system_admin` GUCs per request | 1 new middleware |
| Optional: Alembic | 1 new revision `msp_serial_number` if D15 evolves | Adds `devices.serial_number` |
| Optional: Services | `Inventory.register` accepts `serial_number` if enabled | Additive |

Total new files (RLS-only): **1 Alembic revision + 1 middleware**.
Total new files (with optional D15): **+1 Alembic revision**.

---

## 2. Postgres RLS — D12

### 2.1 Middleware — `backend/app/core/rls_middleware.py`

```python
class RLSSessionMiddleware(BaseHTTPMiddleware):
    """MSP: Phase 6. Sets Postgres GUCs the RLS policies reference.
    Emits `SET LOCAL app.user_id = ...` and `app.is_system_admin = ...` at
    request start. SQLite: no-op.
    """
    async def dispatch(self, request, call_next):
        user = extract_user(request)   # from JWT
        with get_session() as db:
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SET LOCAL app.user_id = :uid"), {"uid": user["id"]})
                db.execute(text("SET LOCAL app.is_system_admin = :sa"),
                           {"sa": "true" if user.get("is_system_admin") else "false"})
        return await call_next(request)
```

Wired into `main.py` in the middleware stack, before authentication.

### 2.2 Alembic migration — `msp_rls`

`backend/migrations/versions/<yyyyMMddHHmm>_msp_rls.py`

Only runs on Postgres — guard with `if bind.dialect.name == 'postgresql': ...` and no-op on SQLite (so dev environments remain functional).

```python
def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("ALTER TABLE sites          ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE device_groups  ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE devices        ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_logs     ENABLE ROW LEVEL SECURITY")

    # sites: visible if system-admin, OR user has any grant for the site.
    #        Base Infrastructure hidden entirely from non-system-admins (D14).
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
        );
    """)

    # device_groups: visible if system-admin, OR user has a site-scoped grant
    #                covering the group's site, OR a group-scoped grant on this group.
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
        );
    """)

    # devices: same rule, resolved via the device's group's site.
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
        );
    """)

    # audit_logs: scoped by the resource's implicit site (matches app-level scoping
    # introduced in Phase 4 T4.4).
    # For simplicity, allow all rows whose resource has a device_group visible to
    # the caller. Rows about resource='user' need a separate JOIN — expressed with
    # a large policy or moved to app-only enforcement; recommended: keep app-layer
    # scoping (Phase 4) for user rows and RLS-scope only device/device_group/site rows.
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
            -- Extend with analogous branches for resource IN ('device_group','site')
        );
    """)

    # Write policies: use the same predicate. For simplicity, replicate as
    # FOR INSERT/UPDATE/DELETE. In practice the app layer still gates writes
    # via require_scope; RLS here is defense-in-depth for accidental reads.
```

**Rationale for read-only RLS:** the app's `require_scope` dependency already gates writes at the boundary. Adding write policies duplicates that check and risks silent inconsistencies (e.g., an INSERT that succeeds at the app level but is rejected by RLS looks like a bug). Read-side RLS is enough to make ID-guessing completely impossible even on a raw SQL bug.

### 2.3 Tests
`backend/tests/test_msp_rls.py` (Postgres-only; skip on SQLite via marker):
- Set `SET LOCAL app.user_id`, `app.is_system_admin=false` in a fresh session; assert only granted sites/groups/devices are visible.
- Assert Base Infra is invisible to non-system-admins even with grants on other sites.
- Assert system-admin GUC bypasses everything.
- Assert an intentionally-buggy service that skips `require_scope` still cannot return other-site rows.

---

## 3. Optional: Device serial-number uniqueness — D15

Per the user's answer to D15: **not adopted at this time**. Keep this section for future reference; do not run in Phase 6 unless the user later confirms.

### If activated later

- Alembic `msp_serial_number`:
  ```python
  op.add_column("devices",
      sa.Column("serial_number", sa.String(length=128), nullable=True))
  op.create_unique_constraint(
      "uq_devices_serial_number", "devices", ["serial_number"],
      postgresql_where=sa.text("serial_number IS NOT NULL"))
  ```
- `Inventory.register` gains a `serial_number: Optional[str] = None` param.
- Optional discovery scan populates existing rows (out of scope — separate proposal).

---

## 4. Merge strategy

- **Mergeable to `main`?** ✅ **Yes.** Independent of Phase 5 in the code — depends on the schema Phase 5 finalized.
- **PR structure:**
  - PR-A: RLS middleware + `msp_rls` migration (Postgres-only). Recommended.
  - PR-B (optional): `msp_serial_number` migration + `Inventory.register` change. Only when user confirms.
- **Roll-out:** deploy PR-A to staging first; run the RLS test suite; observe query plans (RLS adds an EXISTS subquery to every SELECT — verify sane index usage on `role_assignments`).
- **Rollback:** `alembic downgrade -1` disables RLS and drops policies — safe.

---

## 5. Exit criteria

- [ ] RLS enabled on `sites`, `device_groups`, `devices`, `audit_logs` in prod Postgres.
- [ ] Middleware sets GUCs on every request; verified in a request-trace.
- [ ] RLS test suite passes.
- [ ] Query-plan review shows no unbounded scans on `role_assignments`.
- [ ] `CHANGELOG.md` updated ("Security" section).
- [ ] SQLite dev environments unaffected (RLS migration no-ops on non-Postgres).
