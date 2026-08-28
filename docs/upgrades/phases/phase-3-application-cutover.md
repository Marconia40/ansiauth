# Phase 3 — Application Cutover (feature-flagged)

> **Goal.** Land the entire MSP application layer — new services, new dependency, new endpoints — **behind a single feature flag `MSP_STRICT_HIERARCHY` (default `false`)**. Legacy paths keep working unchanged. Enable the flag in staging, run the snapshot-diff test, then merge.
> **Preconditions:** Phase 2 merged, deployed, verification passing, post-migration report reviewed.
> **Design principles:** see [phase-0-prerequisites.md §1](phase-0-prerequisites.md#1-cross-phase-design-principles-apply-to-every-phase).
> **Companion plan sections:** §10, §12, §16, §17, §20, §32 T3.1–T3.5, decisions D7, D13, D14, D16, D17, D19, D22, D23, D25, D26, D_active_job.

---

## 1. Scope summary — the "compact" part

To honor the user's traceability requirement, this phase adds **exactly**:

| Kind | Count | Names |
|---|---|---|
| New services | 3 | `inventory_service.Inventory`, `role_assignment_service.RoleAssignmentService`, `effective_role.effective_role` |
| New dependency | 1 | `core/scope.require_scope(operation_name)` (colocated with the operation → min-role table) |
| New schemas | 1 file, 3 models | `schemas/role_assignment.py`: `RoleAssignmentCreate`, `RoleAssignmentRead`, `SystemAdminUpdate` |
| New endpoints | 6 | `POST /devices/{name}/move`, `GET /sites/{id}/groups`, `POST/DELETE/GET /users/{id}/grants`, `PUT /users/{id}/system-admin` |
| New settings | 1 flag | `MSP_STRICT_HIERARCHY: bool = False` in `core/config.py` |
| Rewritten endpoints (behind flag) | 3 | `GET /devices`, `GET /device-groups`, `GET /sites` (SQL scope predicate when flag on) |
| Converted endpoints (ESC-1..4, always-on) | 17 | 2 in `devices.py` (get, save), 3 in `jobs.py`, 9 in `ports.py`, 5 in `vlans.py`, 1 in `auth.py` — mechanical `ensure_device_allowed`/`require_role` → `require_scope` swaps. See §7.1. |
| Deleted files | 0 | Legacy `authz.py`, `dependencies.require_role` stay intact — deleted in Phase 5. |

**Absolute limit:** no other new services, dependencies, or files. If a task appears to need a 4th service or 2nd dependency, reject it and inline the logic into one of the three services above.

---

## 2. Feature flag

`backend/app/core/config.py` — extend the existing `Settings` class:
```python
MSP_STRICT_HIERARCHY: bool = Field(default=False, description="MSP: Phase 3 gate")
```

Every new code path either (a) always-on and additive (new endpoints, new services), or (b) toggled by `settings.MSP_STRICT_HIERARCHY`. The toggle points are:
- `api/devices.py::list_devices` — flag-on: use `Inventory.list(user)`; flag-off: legacy path.
- `api/device_groups.py::list_groups` — same.
- `api/sites.py::list_sites` — same.
- `api/devices.py::get_device` — flag-on: `require_scope('read_device')`; flag-off: legacy `ensure_device_allowed`.
- `api/device_groups.py::get_group` — same.
- `api/sites.py::get_site` — same.

Everywhere else, the new endpoints are unconditionally on because they have no legacy equivalent.

---

## 3. New service — `Inventory` — T3.1

### File
`backend/app/services/inventory_service.py`

### Class
```python
class Inventory:
    """MSP: Phase 3. Sole entry point for Device CRUD, listing, and movement.
    Thin wrapper over the DB session; do not add non-Device operations here."""

    def __init__(self, db: Session):
        self.db = db

    # ---------- read ----------
    def get(self, name: str) -> DeviceModel | None: ...
    def list(self, user: dict, *, site_id: int | None = None,
             device_group_id: int | None = None) -> list[DeviceModel]:
        """Scoped list. When settings.MSP_STRICT_HIERARCHY, filters via SQL JOIN
        on role_assignments. Otherwise delegates to legacy allowed_device_names_for."""

    # ---------- write ----------
    def register(self, *, name, host, vendor, platform, username, password,
                 site_id: int, device_group_id: int | None,
                 actor: dict) -> DeviceModel:
        """Resolves device_group_id to site.default_group_id when None.
        Enforces group.site_id == site_id (400 on mismatch)."""

    def move(self, name: str, target_group_id: int, actor: dict) -> DeviceModel:
        """Handles same-site and cross-site moves.
        - Enforces D_active_job (409 if device has non-terminal Job).
        - Delegates authz to require_scope('move_device_same_site' | 'move_device_cross_site').
        - Emits audit row 'move_device' with from/to group + site."""

    def deregister(self, name: str, actor: dict) -> None: ...
```

**Traceability rules:**
- `Inventory` is the *only* class that mutates `devices.device_group_id`.
- No other module owns `move_device` — grep `def move` and only `Inventory.move` appears.
- Legacy `device_service.update_device` remains in Phase 3 but its `site_id` param becomes a deprecated no-op (returns 400 when set and flag is on).

### Wired into `api/devices.py`

Replace the current handlers so each is exactly:
```python
@router.get("")
def list_devices(user = Depends(require_authenticated),
                 site_id: int | None = None, device_group_id: int | None = None,
                 db: Session = Depends(get_db)):
    return [_to_public(d) for d in Inventory(db).list(user,
        site_id=site_id, device_group_id=device_group_id)]

@router.post("/{name}/move", status_code=200)
def move_device(name: str, body: DeviceMove,
                user = Depends(require_scope("move_device")),
                db: Session = Depends(get_db)):
    return _to_public(Inventory(db).move(name, body.device_group_id, actor=user))
```

Every handler = one Inventory call. No branching in the handler.

---

## 4. New service — `RoleAssignmentService` — T3.3a

### File
`backend/app/services/role_assignment_service.py`

### Class
```python
class RoleAssignmentService:
    """MSP: Phase 3. Sole owner of role_assignments rows and users.is_system_admin."""

    def __init__(self, db: Session):
        self.db = db

    def grant(self, *, target_user_id: int, site_id: int,
              device_group_id: int | None, role: str,
              actor: dict) -> RoleAssignmentModel:
        """- Validates role ∈ observer/operator/admin.
        - Validates device_group.site_id == site_id when group set.
        - Authz: actor must be system-admin OR site-admin on site_id (D25: group-admin cannot delegate).
        - Emits audit 'grant_role_assignment'."""

    def revoke(self, grant_id: int, actor: dict) -> None:
        """Same authz as grant; emits audit 'revoke_role_assignment'."""

    def list_for_user(self, user_id: int, viewer: dict) -> list[RoleAssignmentModel]:
        """viewer sees only grants at sites they admin (or all if system-admin,
        or all if viewer.id == user_id)."""

    def set_system_admin(self, target_user_id: int, is_system_admin: bool,
                         actor: dict) -> UserModel:
        """- Authz: actor.is_system_admin required.
        - Guard: cannot demote the last active system-admin (400).
        - Emits audit 'set_system_admin'."""
```

### Wired into `api/users.py`
Four new handlers, each one line delegating to `RoleAssignmentService`:
```python
@router.post("/{user_id}/grants", status_code=201)
def create_grant(user_id: int, body: RoleAssignmentCreate,
                 actor = Depends(require_authenticated),
                 db: Session = Depends(get_db)):
    return RoleAssignmentService(db).grant(target_user_id=user_id, actor=actor,
        site_id=body.site_id, device_group_id=body.device_group_id, role=body.role)

@router.delete("/{user_id}/grants/{grant_id}", status_code=204)
def delete_grant(...): ...

@router.get("/{user_id}/grants")
def list_grants(...): ...

@router.put("/{user_id}/system-admin")
def set_system_admin(...): ...
```

### Compatibility shim — T3.3b
`api/users.py::update_allowed_sites` (existing `PUT /users/{id}/allowed-sites`) is rewritten to:
```python
def update_allowed_sites(...):
    # Deprecated shim — translates to observer-role Site grants.
    # Response header set: Deprecation: true
    with get_session() as db:
        svc = RoleAssignmentService(db)
        # 1. Revoke every existing observer Site-scoped grant for this user.
        # 2. Grant observer on each supplied site_id.
    resp.headers["Deprecation"] = "true"
```
This shim is deleted in Phase 5.

---

## 5. New pure function — `effective_role` — T3.1

### File
`backend/app/services/effective_role.py`

### Signature
```python
def effective_role(db: Session, user: dict, resource_type: str,
                   resource_id: int | str) -> str | None:
    """MSP: Phase 3. Pure resolver — no writes.

    Returns 'super-admin' if user.is_system_admin else one of
    'observer' | 'operator' | 'admin' | None per §10.5.

    resource_type ∈ {'site', 'device_group', 'device'}
    resource_id: int for site/device_group, str for device.
    """
```

Implementation:
1. Fast path: if `user.is_system_admin`, return `'super-admin'`.
2. Resolve `(site_id, device_group_id | None)` from `resource_type`/`resource_id` via a single ORM query:
   - `device` → JOIN devices→device_groups; return `(group.site_id, group.id)`.
   - `device_group` → SELECT id, site_id; return `(site_id, id)`.
   - `site` → return `(id, None)`.
3. Most-specific: `SELECT role FROM role_assignments WHERE user_id=? AND site_id=? AND device_group_id=?` — if hit, return that role.
4. Site-wide: same query with `device_group_id IS NULL`. If hit, return that role.
5. Return `None`.

**No caching in Phase 3.** Cache/materialized view is a later optimization decision.

---

## 6. New dependency — `require_scope` — T3.1

### File
`backend/app/core/scope.py`

### Layout
The **operation → min-role table** lives at module top so `grep OP_MIN_ROLE` shows the full authorization matrix in one place.

```python
# MSP: Phase 3. Operation → minimum required role. Site vs Group scope handled
# by callers of require_scope() by passing the correct operation name.

OP_MIN_ROLE: dict[str, tuple[str, str]] = {
    # operation                       (scope_kind, min_role)
    "read_device":                    ("device",   "observer"),
    "write_device_config":            ("device",   "operator"),   # VLAN/port ops
    "register_device":                ("site",     "admin"),      # site or group scope
    "edit_device":                    ("device",   "admin"),
    "delete_device":                  ("device",   "admin"),
    "move_device_same_site":          ("device",   "operator"),   # D16
    "move_device_cross_site":         ("site",     "admin"),      # D16, both sides
    "read_group":                     ("device_group", "observer"),
    "create_group":                   ("site",     "admin"),
    "delete_group":                   ("device_group", "admin"),   # D19 auto-move handled by service
    "read_site":                      ("site",     "observer"),
    "list_site_groups":               ("site",     "observer"),
    # site create/delete + system-admin grants → require_system_admin, not this table.
}

def require_scope(operation: str):
    """Returns a FastAPI dependency that resolves the target resource from
    the path/body, computes effective_role, and 403s if below OP_MIN_ROLE."""
```

Resource-resolution rules (baked into `require_scope`):
- The dependency reads path params first: `{name}` → device; `{group_id}` → device_group; `{site_id}` → site.
- If none matches, it reads the request body for `device_group_id`, `site_id` in that order.
- If the operation is `move_device_cross_site`, the dependency checks *both* source and target sites and takes the min of the two effective roles.

### Companion dependencies (same file)
```python
def require_authenticated(...): ...      # replaces bare Depends(get_current_user) call sites
def require_system_admin(...): ...       # for site create/delete + PUT /users/{id}/system-admin
```

**Compact-and-traceable win:** every scoped endpoint reads `Depends(require_scope("<op>"))`. To find out what any endpoint requires: `grep "require_scope" backend/app/api/`, cross-ref against `OP_MIN_ROLE`.

---

## 7. Endpoint changes (summary — full list in [MSP_IMPLEMENTATION_PLAN.md §19](../../MSP_IMPLEMENTATION_PLAN.md#19-api-changes))

| Endpoint | Handler change | Auth |
|---|---|---|
| `GET /api/v1/devices` | Flag-on: `Inventory.list(user)` | `require_authenticated` |
| `GET /api/v1/devices/{name}` | Flag-on: `Inventory.get + require_scope('read_device')` | `require_scope("read_device")` |
| `POST /api/v1/devices` | `Inventory.register` — `site_id` req, `device_group_id` opt | `require_scope("register_device")` |
| `PUT /api/v1/devices/{name}` | Reject `site_id` in body; no other change | `require_scope("edit_device")` |
| `DELETE /api/v1/devices/{name}` | `Inventory.deregister` | `require_scope("delete_device")` |
| `POST /api/v1/devices/{name}/move` | **New.** `Inventory.move`. Same-site vs cross-site chosen inside `require_scope`. | `require_scope("move_device_same_site")` or `..._cross_site` |
| `GET /api/v1/device-groups` | Flag-on: `DeviceGroupService.list(user)` (adds `list` method) | `require_authenticated` |
| `DELETE /api/v1/device-groups/{id}` | **D19**: auto-move devices to Default; return `{moved_devices: [...]}` | `require_scope("delete_group")` |
| `GET /api/v1/sites` | Flag-on: `SiteService.list(user)` | `require_authenticated` |
| `GET /api/v1/sites/{id}/groups` | **New.** | `require_scope("list_site_groups")` |
| `POST /api/v1/sites`, `DELETE /api/v1/sites/{id}` | Base-Infra deletion always 400 | `require_system_admin` |
| `POST /api/v1/users` | Body drops `role` + `allowed_site_ids`; accepts `initial_grants: [...]` | `require_system_admin` OR site-admin (per §12.3) |
| `PUT /api/v1/users/{id}` | **D26**: split — profile fields + password/email; `is_active` (system-admin only) | see D26 in phase-0 |
| `PUT /api/v1/users/{id}/system-admin` | **New.** | `require_system_admin` |
| `POST/DELETE/GET /api/v1/users/{id}/grants` | **New.** | see `RoleAssignmentService` authz |
| `PUT /api/v1/users/{id}/allowed-sites` | **Deprecated shim** T3.3b — writes observer grants | header `Deprecation: true` |

---

## 7.1 T3.4b — Convert remaining legacy-authz callers (ESC-1..4 from Phase 0 grep audit)

The Phase 0 grep audit (`docs/upgrades/phases/artifacts/phase0-grep-audit.txt`) surfaced 17 endpoints that use legacy `ensure_device_allowed` / `require_role` and are not covered by T3.1–T3.4. All of them are **mechanical one-line swaps** — the target dependency already exists in `core/scope.py::OP_MIN_ROLE`.

**These conversions are always-on (not flag-gated)** because they only change *how* authz runs, not *who* is allowed. The old `ensure_device_allowed(user, name)` returned 403 iff the user was not permitted; `require_scope("read_device")` returns 403 iff `effective_role(...)` < observer. Once Phase 2 has populated `role_assignments`, the two must be equivalent — that equivalence is what test T3.5 (snapshot-diff) proves.

### ESC-1 — `backend/app/api/jobs.py`

| Line | Current | Target |
|---|---|---|
| `:73-95` | `list_jobs(site_id, allowed_devices=authz.allowed_device_names_for(user))` | Rewrite `job_service.list_jobs` to accept `user` and JOIN through `role_assignments`. Handler drops `allowed_devices` param. |
| `:118` | `authz.ensure_device_allowed(current_user, job.device)` inside `retry_job` | `Depends(require_scope("write_device_config"))` on the endpoint; delete the imperative line. |
| `:137` | Same, inside `cancel_job` | Same treatment. |

Note: `list_jobs` cannot use `require_scope` at the endpoint layer (there is no single resource in the path). Push the SQL scope predicate into `job_service.list_jobs` — same pattern as `Inventory.list`.

### ESC-2 — `backend/app/api/ports.py` (9 endpoints)

| Line | Current dep | Target dep |
|---|---|---|
| `:164` | `require_role("observer")` + `ensure_device_allowed` | `require_scope("read_device")` |
| `:252, :298, :339, :383, :428, :488, :530` (write endpoints) | `require_role("operator")` + `ensure_device_allowed` | `require_scope("write_device_config")` |
| `:180, :263, :308, :350, :394, :444, :498, :540` | imperative `authz.ensure_device_allowed(...)` lines | delete (the dep now enforces) |

Every handler goes from ~6 lines to ~4 lines after the swap. No behavior change.

### ESC-3 — `backend/app/api/vlans.py` (5 endpoints)

| Line | Current dep | Target dep |
|---|---|---|
| `:33` (list_vlans) | `require_role("observer")` + `ensure_devices_allowed` | `require_scope("read_device")` |
| `:91` (get_vlan) | `require_role("observer")` + `ensure_device_allowed` | `require_scope("read_device")` |
| `:119, :145` (create/update) | `require_role("admin"/"operator")` + `ensure_devices_allowed` | `require_scope("write_device_config")` |
| `:38, :59, :102, :129, :156` | imperative `ensure_device[s]_allowed(...)` lines | delete |

Corner case: `set_vlan` operates on a **batch** of devices. `require_scope` at the endpoint layer only handles one target. Handle by:
- Keep `require_scope("write_device_config")` on the endpoint (validates the *first* / any device — irrelevant since the batch is validated by the loop).
- Inside `vlan_service.set_vlan`, iterate the device list and call `effective_role(user, 'device', name)` for each; if any returns below operator, raise 403 with the specific device name in the message.
- No new dependency needed — the pure function is reused.

### ESC-4 — `backend/app/api/auth.py::unlock_account`

| Line | Current | Target |
|---|---|---|
| `:156` | `Depends(require_role("admin"))` | `Depends(require_system_admin)` |

**Reason:** account unlock is a security-sensitive op with no site scope. Promoting to `require_system_admin` matches D26's philosophy: activation/deactivation and lockout management live at the system level.

**Not a change in behavior for the current single-admin deployment (D24)**: today's `admin` user becomes `is_system_admin=TRUE` after Phase 2, so they still pass this gate.

### Also folded into T3.4b — 2 devices.py callers not covered above

Line references from grep audit §1.5:
- `backend/app/api/devices.py:50` — `ensure_device_allowed` inside `get_device`. Handled by `require_scope("read_device")` — same swap as ESC-2/3.
- `backend/app/api/devices.py:147` — `ensure_device_allowed` inside `save_device_config`. Handled by `require_scope("write_device_config")`.

Both were implied by the §7 endpoint table but adding the explicit swap here so the T3.4b work-list is self-contained.

### Tests for T3.4b

Regression-only — no new behavior to prove. The existing `test_site_scoped_rbac.py`, `test_ports_api.py`, `test_vlans.py`, `test_jobs.py`, `test_port_write_api.py`, `test_port_service.py` must all still pass. Snapshot-diff (T3.5) provides the cross-check that the new dep chain returns the same set of allowed devices as the old.

### Traceability check after T3.4b lands

After T3.4b:
```
grep -rn "ensure_device_allowed\|ensure_devices_allowed" backend/app/
```
should return only the definitions in `backend/app/core/authz.py`. Zero remaining callers in `backend/app/api/`. If any survive, T3.4b is incomplete.

---

## 8. Service-layer edits (concrete)

`backend/app/services/site_service.py`:
- `create_site` (currently a single INSERT): wrap steps 2–4 of §15 in one `db.begin()` transaction — INSERT site, INSERT default group, UPDATE `sites.default_group_id`. Emits two audit rows.
- `delete_site` — reject if `kind == 'BASE_INFRASTRUCTURE'` (400); reject if any device exists in the site (409); when only empty groups remain, cascade-delete them then delete the site. (D5 optional "move to Base Infra default with warning" is exposed via `?force=true` query param → calls `Inventory.move_all_to_base_infra_default(site_id)` before deletion; returns `{moved_devices: [...]}` in the response.)
- `list(user)` — new method; runs the SQL from §12.4 through the ORM.

`backend/app/services/device_group_service.py`:
- `create_group` — set `is_default=False` explicitly; reject if body attempts to set `is_default=True`.
- `rename_group` — **D7**: reject if `is_default=True` (400 "Default group cannot be renamed").
- `set_is_default` — **D7**: does not exist. Never expose.
- `delete_group` — **D19**: reject if `is_default=True`. Auto-move devices to Site's Default via `Inventory.move`; emit one `msp_auto_move_on_group_delete` audit row per device. Return `{deleted_group_id, moved_devices: [...]}`.
- Delete `add_member` / `remove_member`: **not yet** — Phase 5. Keep functional in Phase 3 for the legacy path; they now call `Inventory.move` internally to keep behavior consistent when both APIs are used.
- `list(user)` — new method; SQL scope predicate.
- `list_group_devices` — read via `devices.device_group_id` (not junction) when flag is on.

`backend/app/services/device_service.py`:
- `create_device` — dispatches to `Inventory.register`.
- `update_device` — flag-on: reject `site_id` in body (400). Rest unchanged.
- `_to_domain` — populates `site_id`/`site_name` from `device.device_group.site` when flag is on; from `device.site` when off.

`backend/app/core/authz.py` — **kept intact** in Phase 3. Legacy call sites (unchanged endpoints) still resolve through it. Its functions get deprecation docstrings ("MSP: removed in Phase 5"). Deletion happens in Phase 5.

---

## 9. Request-flow after Phase 3 (flag = ON)

```
HTTP request
   ↓
FastAPI router (thin, ≤ 5 lines)
   ↓  Depends(require_scope("<op>"))
core/scope.require_scope   ────►  services/effective_role.effective_role
                                     ↓
                                  db.session (one query)
   ↓ (authorized)
services/<Inventory|RoleAssignmentService|site_service|device_group_service>
   ↓
db/models  ─►  Postgres/SQLite
```

Every scoped endpoint fits this shape. No handler contains authorization logic. No service inspects `settings.MSP_STRICT_HIERARCHY` except the three list endpoints during transition.

## 10. Request-flow after Phase 3 (flag = OFF)

Same as today. Legacy `require_role` and `authz.py` still run. New endpoints (`/move`, `/grants`, `/system-admin`, `/sites/{id}/groups`) are unconditionally on, but non-mutation of the legacy paths guarantees zero regression risk.

---

## 11. Tests

- **T3.1 — effective_role unit tests** (`backend/tests/test_msp_effective_role.py`):
  - Juan scenarios (§26.2): admin on A, observer on B + operator on B/G2, admin on C/G1+G2 only.
  - Missing grant → `None`.
  - `is_system_admin=True` → always `super-admin`.
- **T3.2 — scoped list tests** (per API): `test_msp_devices_list_scope.py`, `test_msp_groups_list_scope.py`, `test_msp_sites_list_scope.py` — flag on and off, assert results.
- **T3.3 — grant endpoint tests** (`test_msp_grants_endpoint.py`): create/delete/list/authz-rejection cases from §26.2.
- **T3.3b — compat shim test** (`test_msp_legacy_put_allowed_sites_creates_observer_grants.py`).
- **T3.4 — move endpoint tests** (`test_msp_device_move.py`):
  - same-site as operator: allowed.
  - cross-site as operator: rejected.
  - cross-site as site-admin on both: allowed.
  - move with pending Job: 409 (D_active_job).
- **T3.4 — site groups listing** (`test_msp_site_groups_listing.py`).
- **T3.4b — regression only** (ESC-1..4 swaps). No new test file. Every existing `test_site_scoped_rbac.py`, `test_ports_api.py`, `test_vlans.py`, `test_jobs.py`, `test_port_write_api.py`, `test_port_service.py`, `test_rbac.py`, `test_auth.py` must still pass. If any regresses, the swap is wrong (or `role_assignments` backfill is wrong).
- **T3.5 — snapshot-diff test** (`test_msp_authz_snapshot_diff.py`): with `MSP_STRICT_HIERARCHY=true`, for every seeded user assert the visible-devices set equals the legacy result. R3.
- **T3.6 — D19 test** (`test_msp_delete_group_auto_moves_devices.py`).
- **T3.7 — D7 test** (`test_msp_default_group_immutable.py`) — rename/delete/demote all rejected.
- **T3.8 — D14 test** (`test_msp_base_infra_visible_only_to_system_admin.py`).
- **T3.9 — D26 test** (`test_msp_user_profile_edit_scoping.py`).

**Non-regression:** every existing test in `backend/tests/` continues to pass with the flag off. Run twice in CI (flag on + flag off) starting this phase.

---

## 12. Merge strategy

- **Mergeable to `main`?** ✅ **Yes, in one PR or split into 2 (one for the new endpoints, one for the flag-gated list rewrites).** Depends on Phase 2.
- **Behavior with flag default `false`:** identical to today for every legacy endpoint. New endpoints (`/move`, `/grants`, `/system-admin`, `/sites/{id}/groups`) are always available.
- **Enablement roll-out:**
  1. Merge with flag defaulted `false`.
  2. Deploy to staging.
  3. Set `MSP_STRICT_HIERARCHY=true` in staging; run integration + snapshot-diff (T3.5).
  4. If green, keep flag `false` in prod for one release cycle to allow rollback.
  5. Flip to `true` in prod as the **first step of Phase 4** (that's what makes Phase 4 the breaking release).
- **PR checklist:**
  - [ ] 3 new service files + 1 new dependency file + 1 new schema file (no more)
  - [ ] Every touched endpoint handler ≤ 5 executable lines
  - [ ] All Phase-3 tests pass (both flag states)
  - [ ] All existing tests pass with flag off
  - [ ] `CHANGELOG.md` appended
  - [ ] `docs/upgrades/phases/artifacts/phase3-snapshot-diff-<yyyy-mm-dd>.txt` from staging attached

---

## 13. Rollback

- **Flag flip:** set `MSP_STRICT_HIERARCHY=false` — instant rollback to legacy behavior for list/get endpoints. New endpoints keep working but write to `role_assignments` (already populated in Phase 2, harmless to keep adding grants).
- **Full revert:** revert the Phase-3 PR. Alembic schema (Phase 1 + Phase 2) can stay in place; unread columns are harmless.

---

## 14. Exit criteria

- [ ] All Phase-3 tests pass with flag on AND off.
- [ ] Snapshot-diff (T3.5) shows zero discrepancies in staging.
- [ ] Every scoped endpoint uses `require_scope(...)` (not `require_role`) when flag is on.
- [ ] `grep -c "OP_MIN_ROLE\|require_scope" backend/app` returns matches only in `core/scope.py` and `api/` — no other module.
- [ ] `Inventory`, `RoleAssignmentService`, `effective_role` each in one file; grep confirms no duplication.
- [ ] Deprecation warning present in `PUT /users/{id}/allowed-sites` response.
- [ ] **T3.4b:** `grep -rn "ensure_device_allowed\|ensure_devices_allowed" backend/app/api/` returns zero hits.
- [ ] **T3.4b:** `backend/app/api/auth.py::unlock_account` uses `require_system_admin`.
- [ ] `CHANGELOG.md` updated.
