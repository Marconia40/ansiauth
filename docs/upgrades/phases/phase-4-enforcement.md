# Phase 4 — Enforcement (M3) + Frontend Cutover

> **Goal.** Flip `MSP_STRICT_HIERARCHY=true` by default, enforce every MSP invariant at the DB layer (NOT NULL, unique partial indexes, RESTRICT FKs), update the API schemas to the strict shape, and land the frontend changes.
> This is the **breaking release**. Bump minor version. Coordinate BE + FE deploys.
> **Preconditions:** Phase 3 merged, staging soaked with flag=true for at least one week, snapshot-diff clean.
> **Design principles:** see [phase-0-prerequisites.md §1](phase-0-prerequisites.md#1-cross-phase-design-principles-apply-to-every-phase).
> **Companion plan sections:** §11, §19, §21, §23 (M3), §25, §32 T4.1–T4.5, decisions D3, D6, D20, D27.

---

## 1. Scope summary

| Layer | Change |
|---|---|
| Alembic | 1 new revision `msp_enforce` — NOT NULL, partial unique indexes, ondelete tightening |
| Config | Flip `MSP_STRICT_HIERARCHY` default `False` → `True` |
| Schemas | `DeviceCreate.site_id: int` required; `DeviceUpdate.site_id` removed |
| API | Deprecate `POST /device-groups/{id}/members` and `DELETE ... /{device_name}` (still respond, header `Deprecation: true`) |
| Services | `delete_group` no longer references M2M `device_group_members` for D19 logic — reads `devices.device_group_id` directly |
| Audit | Site-admins now see only audit rows scoped to their sites (D27) |
| Frontend | Device form: Site required + Group dropdown reloads on Site change; Users: grants editor; Sites: `[id]` detail page |

No new files (all edits extend Phase 3 additions or existing modules). No new services.

---

## 2. Alembic migration — T4.1

### File
`backend/migrations/versions/<yyyyMMddHHmm>_msp_enforce.py`

### Revision metadata
```python
revision = "e4msp3_enforce"
down_revision = "e2msp2_backfill"
```

### `upgrade()`

```python
def upgrade() -> None:
    # 1. NOT NULL on the columns Phase 2 populated
    op.alter_column("devices", "device_group_id",
                    existing_type=sa.Integer(), nullable=False)
    op.alter_column("device_groups", "site_id",
                    existing_type=sa.Integer(), nullable=False)
    op.alter_column("sites", "default_group_id",
                    existing_type=sa.Integer(), nullable=False)

    # 2. Partial unique indexes — enforce the invariants
    op.create_index(
        "ux_sites_single_base_infra", "sites", ["kind"], unique=True,
        postgresql_where=sa.text("kind = 'BASE_INFRASTRUCTURE'"),
        sqlite_where=sa.text("kind = 'BASE_INFRASTRUCTURE'"),
    )
    op.create_index(
        "ux_device_groups_one_default_per_site", "device_groups", ["site_id"], unique=True,
        postgresql_where=sa.text("is_default = TRUE"),
        sqlite_where=sa.text("is_default = 1"),
    )

    # 3. Group name unique per site (D6)
    op.drop_constraint("uq_device_group_name", "device_groups", type_="unique")
    op.create_unique_constraint("uq_device_group_site_name", "device_groups",
                                ["site_id", "name"])

    # 4. FK ondelete tightening
    #    device_groups.site_id: SET NULL → RESTRICT
    op.drop_constraint("fk_device_groups_site_id_sites", "device_groups", type_="foreignkey")
    op.create_foreign_key("fk_device_groups_site_id_sites",
        "device_groups", "sites",
        ["site_id"], ["id"], ondelete="RESTRICT")
```

### Pre-M3 verification (fail-fast)
Before flipping NOT NULL, the migration re-runs the M2 verification queries. If any returns non-zero, abort with a clear error and instructions to run M2 first.

### Tests
- `backend/tests/migrations/test_msp_m3_enforces_constraints.py`
  - Seed a device with `device_group_id=NULL`; assert M3 upgrade fails.
  - After M3 completes, attempt to INSERT a second `kind='BASE_INFRASTRUCTURE'` row → IntegrityError.
  - After M3 completes, attempt to INSERT two `is_default=TRUE` rows in the same site → IntegrityError.
  - After M3 completes, attempt to INSERT two groups named `Default` in the same site → IntegrityError; two `Default` in *different* sites → OK (D6).

---

## 3. Feature-flag flip — T4.5

`backend/app/core/config.py`:
```python
MSP_STRICT_HIERARCHY: bool = Field(default=True, description="MSP: Phase 4 default")
```

Keep the flag as an escape hatch for one release. Phase 5 removes it entirely.

---

## 4. Schema & endpoint changes — T4.2 / T4.3

### 4.1 `backend/app/schemas/device.py`
```python
class DeviceCreate(BaseModel):
    name: str
    host: str
    vendor: str
    platform: str
    username: str
    password: str
    site_id: int                          # ← was Optional[int]
    device_group_id: Optional[int] = None # NEW; None → Site's Default

class DeviceUpdate(BaseModel):
    host: Optional[str] = None
    vendor: Optional[str] = None
    platform: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    # site_id removed — force callers to POST /devices/{name}/move
    # device_group_id NOT added here per D20 (use /move)

class DevicePublic(BaseModel):
    # ... existing ...
    device_group_id: int
    device_group_name: str
    site_id: int              # derived from device.device_group.site_id
    site_name: str            # derived
```

`api/devices.py::create_device` — no branching; still one call to `Inventory.register`. The service enforces `group.site_id == body.site_id` (400 mismatch).

`api/devices.py::update_device` — rejects `site_id` and `device_group_id` in body (400 with hint "use POST /devices/{name}/move").

### 4.2 M2M endpoint deprecation
`api/device_groups.py::add_member` and `remove_member`:
- Add response header `Deprecation: true`.
- Body handling unchanged, but internally redirect to `Inventory.move`. `add_member` moves the device into the target group. `remove_member` moves the device to the site's Default group.
- Tests: `backend/tests/test_msp_deprecated_member_endpoints_still_work.py`.

Actual deletion of `add_member`/`remove_member` and their DB junction happens in Phase 5.

### 4.3 D19 handler (moved out of the flag)
`api/device_groups.py::delete_group` — as of Phase 3 already returns `{deleted_group_id, moved_devices: [...]}`; Phase 4 removes the M2M-reading fallback branch since `devices.device_group_id` is now NOT NULL.

### 4.4 D27 — audit scoping
`api/audit.py::list_audit`:
- `is_system_admin` → returns everything (unchanged).
- Otherwise: JOIN through the resource:
  - `resource='device'` → JOIN devices→device_groups→sites, filter by `role_assignments`.
  - `resource='device_group'` → JOIN device_groups→sites, filter.
  - `resource='site'` → filter by `role_assignments.site_id`.
  - `resource='user'` → filter to sites where the audited user holds any grant.
  - Others (`resource='job'`, `resource='vlan'`, etc.) → resolve to the underlying device, then apply the device predicate.
- The query is one SQL statement; no post-fetch filtering.
- Test: `backend/tests/test_msp_audit_scoping.py`.

---

## 5. Frontend cutover — T4.4

Framework: Next.js 16 App Router; React Query 5; axios.

### 5.1 Type updates (`frontend/src/types/device.ts`, `.../site.ts`, `.../user.ts`)
- `Device`: add `device_group_id: number`, `device_group_name: string`; `site_id: number` (was optional).
- `Site`: add `kind: 'REGULAR' | 'BASE_INFRASTRUCTURE'`, `default_group_id: number`.
- `User`: add `is_system_admin: boolean`; remove `allowed_site_ids`, add `grants: RoleAssignment[]`.

### 5.2 API client (`frontend/src/services/api.ts`)
Delete: `addGroupMember`, `removeGroupMember` (post-Phase-5); mark deprecated now.
Add:
```ts
moveDevice(name: string, deviceGroupId: number): Promise<Device>
listSiteGroups(siteId: number): Promise<DeviceGroup[]>
listGrants(userId: number): Promise<RoleAssignment[]>
grant(userId: number, body: {site_id: number; device_group_id?: number; role: string}): Promise<RoleAssignment>
revoke(userId: number, grantId: number): Promise<void>
setSystemAdmin(userId: number, isSystemAdmin: boolean): Promise<User>
```

### 5.3 Pages

- `devices/page.tsx`
  - Registration modal: **Site dropdown required** (scoped to `visibleSites`). On change → fetch `listSiteGroups(siteId)` → populate Group dropdown, preselect Default group. Remove the ability to leave Site blank.
  - Row action: "Move…" — opens Group picker scoped to allowed Sites.
  - Remove the "clear Site" affordance.

- `device-groups/page.tsx`
  - D18 answer noted: user is still deciding UX. Interim: keep the page as-is but source `groupDevicePool` from `device.device_group_id` (not the M2M pool). No structural rewrite in Phase 4.

- `sites/page.tsx`
  - Add `<Badge>` when `kind='BASE_INFRASTRUCTURE'`; hide the Delete button for that row.
  - Add a "Default group" chip per site row.

- `sites/[id]/page.tsx` — **new route**
  - Server component fetches `GET /sites/{id}` + `GET /sites/{id}/groups`.
  - Renders each group with a nested Devices list (`/api/v1/device-groups/{id}/devices`).
  - "Move Device" button per row; opens the same picker.

- `users/page.tsx` — substantial rewrite
  - Remove the `allowed_site_ids` checkbox list.
  - Add a **Grants editor table** with rows `(site, group?, role, actions)`.
  - Add a "System Admin" toggle at the top (visible only to system-admins).
  - New create-user flow: `initial_grants: [...]`.

- `page.tsx` (dashboard)
  - Rename `unassignedCount` widget to "Base Infrastructure device count"; count `device.site_id === baseInfraSite.id` instead of `d.site_id == null`.

### 5.4 Manual FE smoke checklist (no unit-test infra yet)
- [ ] Register device: Site required; Group defaults to Default; submitting without Site is blocked.
- [ ] Site detail page shows groups and devices.
- [ ] Move device across groups (same site): success.
- [ ] Move device across sites: allowed only for site-admin on both.
- [ ] Grant management: create observer grant on Site B, log in as that user, verify visibility.
- [ ] Delete group with devices: dialog shows "Devices will be moved to Default group of this site — proceed?"; on confirm, the response lists moved devices.

---

## 6. Coordinated release sequence

1. **Merge PR.** Migration M3 does NOT run yet.
2. **Deploy backend to staging** with `MSP_STRICT_HIERARCHY=false`. Verify no regression.
3. **Run M3 in staging.** Verify no error; verification queries pass.
4. **Enable `MSP_STRICT_HIERARCHY=true` in staging.** Run integration.
5. **Deploy frontend to staging.** Manual smoke.
6. **Repeat for prod, one region at a time if applicable.**
7. **Announce deprecation** for `POST /device-groups/{id}/members`, `DELETE .../{device_name}`, `PUT /users/{id}/allowed-sites`, and PUT-based site changes. One release window before Phase 5.

---

## 7. Request-flow after Phase 4

Identical to Phase 3 flag-on state. The single change is that `MSP_STRICT_HIERARCHY=true` is the default; the flag itself still exists but the branches only diverge for the three list endpoints (still slated for deletion in Phase 5).

---

## 8. Merge strategy

- **Mergeable to `main`?** ⚠️ **Yes but with coordination.** This is the breaking release.
- **Split PRs strategy** (recommended for reviewability):
  - PR-A: Alembic M3 + config flag flip + backend deprecation headers. Ships M3, does not require FE.
  - PR-B: Schema changes (`DeviceCreate.site_id` required) + FE cutover. Ships together.
  - PR-C: D27 audit scoping (isolated).
- **Combined PR** is acceptable if the team prefers one atomic breaking-change commit.
- **PR checklist per PR:**
  - [ ] Migration or schema change stated in the PR title
  - [ ] `CHANGELOG.md` breaking-change section
  - [ ] Staging deploy proof + M3 verification logs attached
  - [ ] FE smoke checklist attached (PR-B)
- **Version bump:** `1.0 → 1.1` in OpenAPI `info.version` and `frontend/package.json`.

---

## 9. Rollback

- **Config-level:** `MSP_STRICT_HIERARCHY=false` reverts the list/get behavior to legacy. Does **not** revert M3's DB constraints — those stay.
- **DB-level:** `alembic downgrade -1` (undoes M3): drops the partial unique indexes, relaxes NOT NULL, restores `SET NULL` FK. Safe.
- **Full revert:** revert PR-B (FE + schema) first, then downgrade Alembic. Legacy paths still work because Phase 2 backfill guarantees the columns are populated correctly.

---

## 10. Exit criteria

- [ ] Alembic M3 applied cleanly in prod.
- [ ] `MSP_STRICT_HIERARCHY=true` in prod config.
- [ ] All Phase-4 tests pass; Phase-3 tests still pass; existing tests still pass.
- [ ] FE smoke checklist executed on prod-like environment.
- [ ] Deprecation notice in `CHANGELOG.md` for endpoints slated to drop in Phase 5.
- [ ] Version bumped in OpenAPI + package.json.
- [ ] D27 audit scoping active — verified with a manual GET as a site-admin user.
