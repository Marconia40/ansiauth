# Phase 0 — Prerequisites

> **Goal.** Everything that must be true *before* the first line of MSP code is written.
> **No production code changes.**
> **Companion sections in the plan:** [MSP_IMPLEMENTATION_PLAN.md §31](../../MSP_IMPLEMENTATION_PLAN.md#31-proposed-implementation-phases) Phase 0.

---

## 1. Cross-phase design principles (apply to every phase)

Written here once so subsequent phase files can point at this section.

### 1.1 One entry point per capability
Every new MSP capability lands as **one** unit; every endpoint calls **one** service method; that service method delegates to **one** domain function. Concrete manifests:

| Capability | Sole entry point |
|---|---|
| Device CRUD + list + move | `backend/app/services/inventory_service.py::Inventory` (one class) |
| Grant CRUD | `backend/app/services/role_assignment_service.py::RoleAssignmentService` (one class) |
| "Which role does this user have on this resource?" | `backend/app/services/effective_role.py::effective_role(user, resource_type, resource_id)` (one pure function) |
| Endpoint-level authorization | `backend/app/core/scope.py::require_scope(operation)` (one FastAPI dependency, one operation → min-role table colocated in the same module) |
| Bootstrap Base Infrastructure + default groups | `backend/app/services/site_service.py::ensure_base_infrastructure()` (called once from `main.py` lifespan) |

**No per-operation dependencies** (no `require_site_admin_on_device_id`, no `require_operator_on_group`). Instead: one `require_scope("operation_name")` reading the min-role table.

**No per-role helpers** (no `is_admin_of_site`, `is_operator_of_group`). Instead: one `effective_role(...)` call.

### 1.2 Request-flow contract
After Phase 3 lands, every scoped request traces the same shape. Any deviation is a smell.

```
HTTP request
   ↓
FastAPI router (app/api/<resource>.py)          ← 1 endpoint = 1 handler = 1 service call
   ↓  Depends(require_scope("<operation>"))
core/scope.py::require_scope                    ← resolves resource, calls effective_role
   ↓
services/effective_role.py                      ← pure function, JOIN via role_assignments
   ↓
services/<inventory | role_assignment | site | device_group>_service.py
   ↓
db/models.py (SQLAlchemy)
   ↓
Postgres
```

Every phase file states which piece of the shape it touches.

### 1.3 Prefer extend > add
Before adding a new file, verify no existing file owns the capability. New files this migration adds — total: **5** (`inventory_service.py`, `role_assignment_service.py`, `effective_role.py`, `core/scope.py`, `schemas/role_assignment.py`). Everything else extends existing modules.

### 1.4 Traceability aids

- Every new service method carries a `# MSP: <phase>` tag comment on its first line during the migration so a `grep -rn "# MSP:"` shows the full delta.
- Every migration filename gets the `msp_` prefix: `msp_additive.py`, `msp_backfill.py`, `msp_enforce.py`, `msp_cleanup.py`, `msp_rls.py`.
- Every audit log row emitted by MSP-migration code uses `action='msp_migration_*'`.
- The `MSP_STRICT_HIERARCHY` env var is the single feature flag; **no** additional MSP-related flags are introduced.

### 1.5 Anti-patterns to reject
- ❌ Post-fetch Python filtering. All scoping is SQL predicates through `role_assignments`.
- ❌ Denormalized `devices.site_id`. Site is always derived via `device.device_group.site`.
- ❌ New coarse `require_role(...)` on endpoints that touch a scoped resource. Use `require_scope(...)`.
- ❌ Adding new roles. Only `observer / operator / admin` at scope; `is_system_admin` on the user row.
- ❌ Splitting `Inventory` into `device_register_service`, `device_move_service`, `device_delete_service`.

---

## 2. Decision confirmations (all answered in `docs/prompts/answers.txt`)

Answers that alter the MSP_IMPLEMENTATION_PLAN.md recommendations are called out here so subsequent phase files can reference them.

| Decision | Answer | Impact on later phases |
|---|---|---|
| D0 | D0a — MSP prompt supersedes | Baseline for the whole plan. |
| D1 | Yes, path is device→device_group→site (no denorm) | Phase 1: `devices` gets `device_group_id`, no `site_id` denorm. |
| D2 | Same `Site` entity with `kind` | Phase 1: `SiteModel.kind`. |
| D3 | Yes; `site.name` must remain globally unique | Phase 4: keep unique constraint on `sites.name`; user rename permitted. |
| D5 | 409 if devices present; **allow admin override that moves devices to Base Infra's default group with a warning** | Phase 3/4: `site_service.delete_site(force=False)`; `force=True` invokes `inventory.move_all_to_base_infra_default(site_id)`. |
| D6 | Per-site unique group name | Phase 4: unique `(site_id, name)`. |
| **D7** | **Default Group is immutable: cannot be renamed, deleted, or demoted** | **Overrides plan §10.2.** Phase 3: `device_group_service.rename` rejects if `is_default=True`; `delete` and `set_is_default(False)` reject the same. |
| D8 | "Remove from group" = move to Site's Default | Phase 4: deprecated M2M `DELETE members` alias forwards to move. |
| D9 | Keep `name`, use `id` as FK | Phase 1: FKs are numeric ids; `name` stays unique. |
| **D12** | **Yes — implement Postgres RLS** | **Phase 6 is now REQUIRED, not optional.** RLS lands as `backend/migrations/versions/<ts>_msp_rls.py` after M4. |
| D13 | Yes, 403 for out-of-scope (not 404) | Phase 3: `require_scope` raises 403. |
| **D14** | **Only `is_system_admin` may see Base Infrastructure** | **Overrides plan §12.5.** Phase 2: **do not** auto-grant Base Infra observer to legacy users during migration Step 6c. Instead, log those users to a report so operator can manually decide (Base Infra is management-only). |
| D15 | Keep `name`; internal `id` also unique; no SN/MAC yet | Phase 1: no `serial_number` in M1. Phase 6: `serial_number` remains optional. |
| **D16** | Same-site move: `operator` in both groups. Cross-site move: `site-admin` in both sites | **Overrides plan §12.3 same-site row.** `require_scope('move_device_same_site')` accepts operator; `require_scope('move_device_cross_site')` requires site-admin. |
| D17 | Yes, cross-site moves allowed (with D16 gate) | Phase 3: `Inventory.move` handles both branches. |
| **D19** | Admins can delete groups; devices auto-moved to Default with a message | **Overrides plan D19.** Phase 3: `device_group_service.delete_group(id)` always auto-moves devices to Site's Default and returns a `moved_devices: [...]` field in the response body. No `force=true` param needed. |
| D22 | Yes, create `CHANGELOG.md` | Phase 0 T0.5 (below). |
| D_active_job | Yes, reject move if device has pending/running Jobs | Phase 3: `Inventory.move` raises 409 when non-terminal Jobs exist. |
| D23 | Yes, audit every grant mutation | Phase 3: `RoleAssignmentService.grant/revoke` emit audit rows. |
| D24 | Only one admin user; promote to `is_system_admin=TRUE` | Phase 2 Step 6a: single-user promotion. Post-migration report still emitted for the operator. |
| D26 | Own password/email: user themselves. Site-admin can edit profile of users grant-scoped to their Site. Deactivation: system-admin only (cross-site consequences noted) | Phase 3: `PUT /users/{id}` split — profile fields (self or scoped site-admin) vs `is_active` (system-admin only). |
| D27 | Site-admins see audit only for their Sites | Phase 4 (or Phase 6): `GET /api/v1/audit` filters by resolved site scope. |

---

## 3. Prerequisite tasks (execute before Phase 1)

### T0.1 — Grep audit
Enumerate every caller of these symbols; cross-check against §20.2 of the plan; document any caller not covered.
```bash
grep -rn "DeviceGroupMemberModel\|UserAllowedSiteModel" backend/
grep -rn "device_group_members\|user_allowed_sites" backend/
grep -rn "is_unrestricted\|allowed_device_names_for\|allowed_site_ids_for" backend/
grep -rn "set_user_allowed_sites\|get_user_allowed_sites" backend/
grep -rn "ensure_device_allowed\|ensure_devices_allowed" backend/
grep -rn "\.site_id" backend/app/services backend/app/api backend/app/models
```
Deliverable: `docs/upgrades/phases/artifacts/phase0-grep-audit.txt` (create when running).

### T0.2 — DB snapshot
```bash
pg_dump ansiauth > /backup/pre_msp_$(date +%F).sql   # production Postgres
cp backend/app/db/app.db /backup/pre_msp_dev.sqlite  # dev SQLite
```

### T0.3 — Alembic head confirmation
```bash
docker compose exec backend alembic current
```
Expected: `d8a5f2c1b630` (drop_sqlite_audit_trigger).

### T0.4 — Test baseline
```bash
docker compose exec backend pytest backend/tests/ -q
```
Record pass/fail count so every subsequent phase's `pytest` output can be compared line-for-line.

### T0.5 — Create `CHANGELOG.md` (D22)
- **File:** `CHANGELOG.md` (repo root).
- **Content:** Keep-a-Changelog format; opening entry:
  ```
  ## [Unreleased] — MSP migration
  ### Added
  - (Phase 1) Additive schema for MSP hierarchy: sites.kind, sites.default_group_id,
    device_groups.is_default, devices.device_group_id (all nullable).
  ```
- Each subsequent phase appends its own bullets.

### T0.6 — Multi-group device audit (R7)
```sql
SELECT device_name, COUNT(*) AS group_count
  FROM device_group_members
GROUP BY device_name
HAVING COUNT(*) > 1;
```
If **any** row returns, escalate to user before Phase 4 (M2M removal is a data-loss event for these).

### T0.7 — Base-Infrastructure name collision check (R4)
```sql
SELECT id, name FROM sites WHERE name IN ('Base Infrastructure', 'base_infrastructure');
```
If a user-created row exists with that name, coordinate a rename before M2.

---

## 4. Merge strategy for this phase

- **Mergeable to `main`?** Yes — this phase writes **no** production code. It only produces the grep audit artifact and `CHANGELOG.md`.
- **PR contents:** `CHANGELOG.md`, `docs/upgrades/phases/artifacts/phase0-grep-audit.txt`.
- **Green-main gate:** existing test suite unchanged.

---

## 5. Exit criteria

- [ ] All answers in `docs/prompts/answers.txt` confirmed applied to `MSP_IMPLEMENTATION_PLAN.md` §30 (already true).
- [ ] Grep-audit artifact exists.
- [ ] DB snapshot exists at a known path.
- [ ] `CHANGELOG.md` created.
- [ ] Test baseline recorded.
- [ ] R7 escalation (if triggered) resolved with user.
- [ ] R4 escalation (if triggered) resolved with user.
