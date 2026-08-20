# Phase 2 — Data Backfill (M2)

> **Goal.** Populate every new column that Phase 1 added, on every existing row. Verify no invariant violations. Idempotent so it can be rerun on non-prod snapshots.
> **Preconditions:** Phase 1 merged and deployed everywhere (schema present).
> **Design principles:** see [phase-0-prerequisites.md §1](phase-0-prerequisites.md#1-cross-phase-design-principles-apply-to-every-phase).
> **Companion plan sections:** §23 (M2), §24, §32 T2.1–T2.2, decisions D5, D14, D19, D24.

---

## 1. Scope summary

| Layer | Change | Notes |
|---|---|---|
| Alembic | 1 new revision `msp_backfill` | Data-only; **no schema changes**. |
| Services | None | The migration operates directly on tables via `op.execute`. |
| API | None | — |
| Frontend | None | — |
| Reports | 1 markdown report generated post-migration | Lists user promotions + ambiguous device assignments. |

Total new files: **1** (Alembic revision) + **1** post-migration report script.
Total modified files: **0**.

Rationale for keeping this outside services: the migration must be reproducible from *bare Alembic*, without importing the app. Application services will read the new columns starting Phase 3.

---

## 2. Alembic migration — T2.1

### File
`backend/migrations/versions/<yyyyMMddHHmm>_msp_backfill.py`

### Revision metadata
```python
revision = "e2msp2_backfill"
down_revision = "e1msp1_additive"
```

### Idempotency contract

Every statement is either:
- an `INSERT ... WHERE NOT EXISTS`, or
- an `UPDATE ... WHERE <target column> IS NULL`.

Running the migration twice must produce the same DB state as running it once. This is enforced by test `test_msp_m2_idempotent.py` (below).

### `upgrade()` — six steps, mirror §24

Each step is a `op.execute(sa.text(...))` block. Consolidated pseudo-code:

#### Step 1 — Base Infra site + default group
```sql
INSERT INTO sites (name, description, kind, created_at, updated_at)
     SELECT 'Base Infrastructure', 'System-managed base infrastructure site.',
            'BASE_INFRASTRUCTURE', now(), now()
      WHERE NOT EXISTS (SELECT 1 FROM sites WHERE kind = 'BASE_INFRASTRUCTURE');

INSERT INTO device_groups (name, description, site_id, is_default, created_at)
     SELECT 'Default', 'Default group for Base Infrastructure',
            s.id, TRUE, now()
       FROM sites s
      WHERE s.kind = 'BASE_INFRASTRUCTURE'
        AND s.default_group_id IS NULL;

UPDATE sites
   SET default_group_id = (
       SELECT g.id FROM device_groups g
        WHERE g.site_id = sites.id AND g.is_default = TRUE
   )
 WHERE default_group_id IS NULL AND kind = 'BASE_INFRASTRUCTURE';
```

Note: **Phase 1's `ensure_base_infrastructure()` may have already created these rows.** The `NOT EXISTS` / `IS NULL` clauses make the migration a no-op in that case.

#### Step 2 — Default group for every regular site
```sql
INSERT INTO device_groups (name, description, site_id, is_default, created_at)
     SELECT 'Default',
            'Default group for site ' || s.name,
            s.id, TRUE, now()
       FROM sites s
      WHERE s.kind = 'REGULAR'
        AND s.default_group_id IS NULL;

UPDATE sites
   SET default_group_id = (
       SELECT g.id FROM device_groups g
        WHERE g.site_id = sites.id AND g.is_default = TRUE
   )
 WHERE default_group_id IS NULL AND kind = 'REGULAR';
```

**D6 collision handling:** if a user-created group is already named `'Default'` in the same site, prefer to promote it (`UPDATE device_groups SET is_default = TRUE`) rather than insert a duplicate name. Concrete SQL:
```sql
-- Promote an existing 'Default'-named group, if any, before insert.
UPDATE device_groups
   SET is_default = TRUE
 WHERE name = 'Default' AND is_default = FALSE
   AND site_id IN (SELECT id FROM sites WHERE default_group_id IS NULL);
```
Runs *before* the INSERT block above; both are idempotent.

#### Step 3 — Assign every device to a device_group

Three sub-steps as in the plan §24 Step 3. Consolidated:

```sql
-- 3a: site-less devices → Base Infra's default
UPDATE devices
   SET device_group_id = (
       SELECT default_group_id FROM sites WHERE kind = 'BASE_INFRASTRUCTURE'
   )
 WHERE device_group_id IS NULL
   AND site_id IS NULL;

-- 3b: devices with exactly one group membership in their site → that group
--     (uses a CTE; Postgres and SQLite ≥3.35 both support this)
WITH single_group AS (
  SELECT m.device_name, MIN(m.group_id) AS group_id
    FROM device_group_members m
    JOIN device_groups g ON g.id = m.group_id
    JOIN devices d ON d.name = m.device_name
   WHERE d.site_id = g.site_id
   GROUP BY m.device_name
  HAVING COUNT(DISTINCT m.group_id) = 1
)
UPDATE devices AS d
   SET device_group_id = sg.group_id
  FROM single_group AS sg
 WHERE d.name = sg.device_name
   AND d.device_group_id IS NULL;

-- 3c: remainder (ambiguous or zero memberships within a site) → site's default
UPDATE devices
   SET device_group_id = s.default_group_id
  FROM sites s
 WHERE devices.site_id = s.id
   AND devices.device_group_id IS NULL;
```

#### Step 4 — Audit-log ambiguous assignments (R1 mitigation)

For every device that Step 3c touched **and** was originally a member of more than one group in its site, emit one immutable `audit_logs` row so history is preserved. Implemented as a single INSERT ... SELECT that runs after Step 3c and consults the pre-migration `device_group_members` snapshot:

```sql
INSERT INTO audit_logs (actor_username, action, resource, resource_id, details, created_at)
SELECT
    'msp_migration' AS actor_username,
    'msp_migration_ambiguous_group_assignment' AS action,
    'device' AS resource,
    d.name AS resource_id,
    json_build_object(
        'previous_groups', array_agg(DISTINCT dgm.group_id),
        'assigned_to', d.device_group_id
    )::text AS details,
    now()
  FROM devices d
  JOIN device_group_members dgm ON dgm.device_name = d.name
  JOIN device_groups g ON g.id = dgm.group_id
 WHERE d.site_id = g.site_id
 GROUP BY d.name, d.device_group_id
HAVING COUNT(DISTINCT dgm.group_id) > 1;
```

(On SQLite, replace `array_agg`/`json_build_object` with `group_concat`/hand-built JSON string. Keep the two dialect branches inside `if bind.dialect.name == "postgresql":` in the migration.)

#### Step 5 — Legacy null-site groups (D21a — delete)

Runs *after* Step 3 so no device references them.

```sql
DELETE FROM device_group_members
 WHERE group_id IN (SELECT id FROM device_groups WHERE site_id IS NULL);

DELETE FROM device_groups WHERE site_id IS NULL;
```

#### Step 6 — User grant migration (D24 answer: single admin → system-admin)

```sql
-- 6a: promote existing admins to is_system_admin
UPDATE users SET is_system_admin = TRUE
 WHERE role IN ('admin', 'super-admin');

-- 6b: operator/observer users → Site-scoped role_assignments
INSERT INTO role_assignments
       (user_id, site_id, device_group_id, role, created_at, created_by_user_id)
     SELECT u.id, uas.site_id, NULL, u.role, now(), NULL
       FROM users u
       JOIN user_allowed_sites uas ON uas.user_id = u.id
      WHERE u.role IN ('operator', 'observer')
        AND NOT EXISTS (
            SELECT 1 FROM role_assignments ra
             WHERE ra.user_id = u.id
               AND ra.site_id = uas.site_id
               AND ra.device_group_id IS NULL
        );
```

**D14 answer applied:** the plan's Step 6c (auto-grant observer on Base Infra to users who previously fell through the `site_id IS NULL` fallback) is **NOT executed**. Base Infrastructure is `is_system_admin`-only. Users who lose visibility to previously-Base-Infra devices are enumerated in the post-migration report (§4 below) so the operator can decide per-user.

### Verification queries at the end of `upgrade()`

Wrapped in a helper that raises `RuntimeError` on any failure:

```python
def _assert_zero(bind, sql: str, msg: str):
    n = bind.execute(sa.text(sql)).scalar()
    if n:
        raise RuntimeError(f"MSP M2 verification failed: {msg} (got {n})")

_assert_zero(bind, "SELECT COUNT(*) FROM devices WHERE device_group_id IS NULL",
             "devices with no device_group_id")
_assert_zero(bind, "SELECT COUNT(*) FROM device_groups WHERE site_id IS NULL",
             "device_groups with no site_id")
_assert_zero(bind, "SELECT COUNT(*) FROM sites WHERE default_group_id IS NULL",
             "sites with no default_group_id")
_assert_zero(bind,
             "SELECT COUNT(*) FROM users WHERE role IN ('admin','super-admin') "
             "AND is_system_admin = FALSE",
             "admin/super-admin users not marked is_system_admin")

# exactly-one Base Infra
count = bind.execute(sa.text(
    "SELECT COUNT(*) FROM sites WHERE kind = 'BASE_INFRASTRUCTURE'"
)).scalar()
if count != 1:
    raise RuntimeError(f"MSP M2 verification failed: expected 1 Base Infra, got {count}")
```

### `downgrade()`
Not fully reversible (data loss on Step 5 deletions is permanent; audit rows in Step 4 are protected by the immutability trigger). Downgrade drops the audit rows via a targeted `DELETE ... WHERE action LIKE 'msp_migration_%'` **only if the audit trigger permits it** — otherwise the downgrade prints a warning and continues. Site/group/role_assignments population can be reversed by clearing the columns:
```sql
UPDATE devices  SET device_group_id = NULL;
UPDATE sites    SET default_group_id = NULL;
UPDATE users    SET is_system_admin = FALSE;
DELETE FROM role_assignments;
DELETE FROM device_groups WHERE is_default = TRUE;   -- default groups this mig created
DELETE FROM sites WHERE kind = 'BASE_INFRASTRUCTURE';
```

Downgrade is intended for staging rollback, not production recovery. Real recovery = restore the pre-M2 backup (T0.2).

---

## 3. Tests — T2.1 / T2.2

Under `backend/tests/migrations/`:

- `test_msp_m2_backfills_default_groups.py` — seed 3 sites (all missing default), run M2, assert each has `is_default=TRUE` group and `sites.default_group_id` set.
- `test_msp_m2_backfills_device_groups.py` — seed devices in the three §24 Step 3 categories, run M2, assert each is in the expected group.
- `test_msp_m2_handles_ambiguous_devices.py` — seed a device that is in two groups within one site, run M2, assert the device lands in the site's Default group **and** an `audit_logs` row exists with `action='msp_migration_ambiguous_group_assignment'`.
- `test_msp_m2_deletes_null_site_groups.py` — seed one null-site group with a member, run M2, assert group and its members are gone.
- `test_msp_m2_user_migration.py` — seed 1 admin, 1 super-admin, 2 observers (one with sites, one without), run M2, assert (a) both admins have `is_system_admin=TRUE`, (b) observer with sites has one `role_assignments` row per site, (c) observer without sites has zero grants (D14 correction).
- `test_msp_m2_idempotent.py` — run M2 twice, assert row counts identical after the second run.
- `test_msp_m2_verification_fails_on_orphan.py` — seed a device with `site_id=NULL` **and** manually null Base Infra's `default_group_id` before running M2. Assert `RuntimeError` from `_assert_zero`.
- `test_msp_m2_snapshot_diff.py` (T2.2 — R3 mitigation) — for every user in the fixture, compute `old_visible_devices` from the legacy `authz.allowed_device_names_for(u)` and `new_visible_devices` from a JOIN through `role_assignments`; assert equality **for observer/operator users** (system-admins see everything by definition — trivially equal after 6a).

---

## 4. Post-migration report (D24, D14)

### 4.1 File
`backend/scripts/msp_post_backfill_report.py` (new, one-shot script)

### 4.2 Output
`docs/upgrades/phases/artifacts/msp_backfill_report_<yyyy-mm-dd>.md`

### 4.3 Content
Three tables:
1. **Promoted-to-system-admin users** (D24) — every user where `is_system_admin=TRUE` and prior `role != 'super-admin'` in the pre-M2 snapshot. Operator reviews and demotes where inappropriate (via Phase 3's `PUT /users/{id}/system-admin`).
2. **Users who lost Base-Infra visibility** (D14) — every operator/observer who previously would have matched the `authz.py:78-83` `site_id IS NULL` fallback. Operator decides per user whether to grant Base Infra observer (via Phase 3's `POST /users/{id}/grants`).
3. **Devices assigned via ambiguous multi-group backfill** (R1) — from the `audit_logs` rows emitted in Step 4.

Script is idempotent: safe to rerun; overwrites its output file.

### 4.4 Execution
```bash
docker compose exec backend python -m backend.scripts.msp_post_backfill_report
```

---

## 5. Request-flow after Phase 2

**Still unchanged** from today for regular API traffic. The application code has not yet been rewired to read the new columns — that is Phase 3.

```
API request  →  existing handlers  →  existing services  →  existing authz.py  →  DB
```

The new columns exist, are populated, but are not read anywhere except by the M2 script and (in Phase 3) the new services.

---

## 6. Merge strategy

- **Mergeable to `main`?** ✅ **Yes, as a standalone PR.** Depends on Phase 1.
- **Rationale:** the migration only mutates the new columns Phase 1 added. Application traffic is unchanged.
- **Blast radius:** on first boot after the deploy, Alembic runs M2 automatically. Duration is bounded by `SELECT COUNT(*) FROM devices` + `SELECT COUNT(*) FROM device_group_members`. For a ~10k-device fleet, expect < 30s.
- **Pre-merge dry-run gate:** run the M2 migration against a *staging copy of production data* and inspect the post-migration report. Merge only if:
  - all verification queries pass,
  - D24 promotion list matches expectations (per the user, exactly one user to promote),
  - the "lost Base Infra visibility" list is inspected and either empty or approved.
- **PR checklist:**
  - [ ] `backend/migrations/versions/<ts>_msp_backfill.py`
  - [ ] `backend/scripts/msp_post_backfill_report.py`
  - [ ] All 8 migration tests
  - [ ] `CHANGELOG.md` appended
  - [ ] Staging dry-run artifact linked in the PR body

---

## 7. Rollback

- **Preferred:** restore pre-M2 backup from T0.2. Fastest, no data loss.
- **In-place:** `alembic downgrade -1`. Data-lossy for null-site groups (§Step 5) and for the ambiguous-assignment audit rows (immutability may block deletion).
- **Coexistence:** if only M2 fails but M1 succeeded, `alembic downgrade -1` unwinds M2 only; M1 can stay in place.

---

## 8. Exit criteria

- [ ] Alembic M2 runs cleanly on a snapshot of production data.
- [ ] All 8 Phase-2 tests pass.
- [ ] `SELECT COUNT(*) FROM devices WHERE device_group_id IS NULL` → 0.
- [ ] `SELECT COUNT(*) FROM sites WHERE default_group_id IS NULL` → 0.
- [ ] `SELECT COUNT(*) FROM sites WHERE kind='BASE_INFRASTRUCTURE'` → 1.
- [ ] Snapshot-diff test (T2.2) passes for every seeded user.
- [ ] Post-migration report generated and reviewed by the operator.
- [ ] `CHANGELOG.md` updated with Phase 2 entry.
