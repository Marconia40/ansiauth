# Phase 5 — Cleanup (M4)

> **Goal.** Drop every legacy column, table, endpoint, service function, and code branch that was kept for backward compatibility during Phases 3–4. This is the **point of no return**: the platform can no longer serve pre-MSP request shapes after this.
> **Preconditions:** Phase 4 has soaked in production for at least one release cycle with no reported issues. All API clients migrated off deprecated endpoints (verify via access logs).
> **Design principles:** see [phase-0-prerequisites.md §1](phase-0-prerequisites.md#1-cross-phase-design-principles-apply-to-every-phase).
> **Companion plan sections:** §23 (M4), §32 T5.1–T5.3.

---

## 1. Scope summary

Removal-only phase. No new capability. Zero new services/dependencies/schemas.

| Layer | Removal |
|---|---|
| Alembic | 1 new revision `msp_cleanup` — drops `devices.site_id`, `device_group_members`, `user_allowed_sites`, `users.role` |
| ORM | Delete `DeviceGroupMemberModel`, `UserAllowedSiteModel`; remove `DeviceModel.site_id`, `UserModel.role` |
| Services | Delete `device_group_service.add_member` / `remove_member`; drop the flag-branch in list services |
| Config | Delete `MSP_STRICT_HIERARCHY` from `Settings` and all guarded branches |
| API | Delete deprecated routes (`POST /device-groups/{id}/members`, `DELETE .../{name}`, `PUT /users/{id}/allowed-sites`) |
| AuthZ | Delete legacy helpers in `core/authz.py` (`is_unrestricted`, `allowed_device_names_for`, etc.); keep the module only if any Phase-5-independent helper survives — otherwise delete the file |
| Frontend | Delete `addGroupMember`, `removeGroupMember` from `services/api.ts`; delete related UI branches |
| Docs | Regenerate the domain diagram |

---

## 2. Alembic migration — T5.1

### File
`backend/migrations/versions/<yyyyMMddHHmm>_msp_cleanup.py`

### Revision metadata
```python
revision = "e5msp4_cleanup"
down_revision = "e4msp3_enforce"
```

### `upgrade()`

```python
def upgrade() -> None:
    # 1. Drop devices.site_id — legacy denorm no longer read anywhere
    op.drop_index("ix_devices_site_id", table_name="devices")   # if exists
    op.drop_constraint("fk_devices_site_id_sites", "devices", type_="foreignkey")
    op.drop_column("devices", "site_id")

    # 2. Drop device_group_members M2M — replaced by devices.device_group_id
    op.drop_table("device_group_members")

    # 3. Drop user_allowed_sites — replaced by role_assignments
    op.drop_table("user_allowed_sites")

    # 4. Drop users.role — replaced by role_assignments + is_system_admin
    op.drop_column("users", "role")
```

### `downgrade()` — pragmatic, best-effort
Restoring the dropped columns and tables *without* data loss requires the M4 backup. The `downgrade()` therefore:
1. Recreates the empty tables/columns.
2. Emits a WARNING via `op.execute("SELECT 'M4 downgrade cannot restore data — restore from backup'")`.
3. Does NOT attempt to rebackfill.

**Documented as one-way** in `CHANGELOG.md` and in a `WARNING` block at the top of the migration file.

### Test
`backend/tests/migrations/test_msp_m4_drops_legacy.py`
- Assert post-upgrade: `devices.site_id` column absent (`information_schema.columns`).
- Assert `device_group_members`, `user_allowed_sites` tables absent.
- Assert `users.role` column absent.
- Assert all Phase 4 constraints still present.

---

## 3. Code cleanup — T5.2

### 3.1 `backend/app/db/models.py`
- Delete `class DeviceGroupMemberModel` and `class UserAllowedSiteModel`.
- Delete `DeviceModel.site_id`, `DeviceModel.site`, `DeviceModel.group_members`.
- Delete `UserModel.role`, `UserModel.allowed_sites`.
- Keep `RoleAssignmentModel`, `SiteModel.default_group_id`, `DeviceGroupModel.is_default`, `DeviceModel.device_group_id`, `UserModel.is_system_admin`.

### 3.2 `backend/app/services/device_group_service.py`
- Delete `add_member(group_id, device_name)`.
- Delete `remove_member(group_id, device_name)`.
- Delete any helper that reads `DeviceGroupMemberModel`.

### 3.3 `backend/app/services/device_service.py`
- Delete `update_device`'s legacy `site_id` handling (already 400 in Phase 4; remove the branch entirely).
- Delete `_to_domain`'s legacy site-attribute derivation branch — keep only the `device.device_group.site` path.

### 3.4 `backend/app/services/site_service.py`
- Delete `delete_site`'s legacy device-count query (now guaranteed to route through `device_group.site_id`).

### 3.5 `backend/app/core/authz.py` — file becomes empty or deleted
Delete every function:
- `is_unrestricted`
- `allowed_site_ids_for`
- `allowed_device_names_for`
- `ensure_device_allowed`
- `ensure_devices_allowed`
- `set_user_allowed_sites`
- `get_user_allowed_sites`

If the file has no surviving helper, delete `backend/app/core/authz.py` entirely and remove all imports.

### 3.6 `backend/app/core/config.py`
- Delete `MSP_STRICT_HIERARCHY` field.

### 3.7 `backend/app/api/*`
- `api/devices.py::list_devices` — delete the `if settings.MSP_STRICT_HIERARCHY` branch; keep the flag-on path unconditionally.
- `api/devices.py::get_device` — same.
- `api/device_groups.py::list_groups` — same.
- `api/device_groups.py::get_group` — same.
- `api/device_groups.py::add_member` / `remove_member` — **delete the routes**.
- `api/sites.py::list_sites` — delete the flag branch; keep scoped path.
- `api/sites.py::get_site` — same.
- `api/users.py::update_allowed_sites` — **delete the shim route** `PUT /users/{id}/allowed-sites`.

### 3.8 `backend/app/services/inventory_service.py`
- Delete the `Inventory.list` fallback branch that delegated to legacy `allowed_device_names_for`.

### 3.9 Grep-audit gate
Before merging Phase 5, run:
```bash
grep -rn "MSP_STRICT_HIERARCHY\|DeviceGroupMemberModel\|UserAllowedSiteModel\|allowed_device_names_for\|is_unrestricted\|set_user_allowed_sites\|ensure_device_allowed\|DeviceModel\.site_id\|UserModel\.role" backend/
```
Expected: **zero hits**. Any hit blocks the PR.

### 3.10 Test cleanup
- Delete the flag-off variants of Phase-3 tests (they now assert impossible states).
- Delete `backend/tests/test_msp_deprecated_member_endpoints_still_work.py`.
- Delete `backend/tests/test_msp_legacy_put_allowed_sites_creates_observer_grants.py`.
- Keep every other Phase-3/Phase-4 test — those assert the target behavior.

---

## 4. Frontend cleanup

### `frontend/src/services/api.ts`
- Delete `addGroupMember`, `removeGroupMember`, `updateAllowedSites`.
- Delete the corresponding React Query hooks that wrap them.

### `frontend/src/app/(dashboard)/device-groups/page.tsx`
- Per D18 answer: user is still deciding. Interim decision for Phase 5:
  - If user opts for **repurpose**: refactor the page to be a per-Site group management surface (URL `/sites/[id]/groups` alternative). Keep the flat `/device-groups` route as a redirect.
  - If user opts for **delete**: remove the route entirely; existing bookmarks redirect to `/sites`.
- Confirm D18 at Phase 5 kickoff.

### Manual FE smoke checklist
- [ ] All Phase-4 flows still work.
- [ ] No console reference to removed endpoints (network tab).
- [ ] Users page still renders after `allowed_site_ids` removal.

---

## 5. Diagram regeneration — T5.3

### File
`docs/prompts/new/diagrama_clases_dominio.drawio` (replace) **or** new versioned file `docs/msp/diagrama_clases_msp.drawio` (recommended — keeps history).

### Content
Reflect the model in [MSP_IMPLEMENTATION_PLAN.md §10](../../MSP_IMPLEMENTATION_PLAN.md#10-target-domain-model) and §10.5:

- Remove: `device_sites` join line, orange "acceso directo" line, `Usuario.allowed_sites: int[]`, `DeviceGroupMemberModel`.
- Add: `Site.default_group_id`, `Site.kind`, `DeviceGroup.is_default`, `RoleAssignment` class linked to `Usuario` and both `Site` / `DeviceGroup`.
- Update: `Device` no longer holds `site_id` — link goes via `DeviceGroup`.

Export a fresh PDF alongside the `.drawio` file.

---

## 6. Request-flow after Phase 5

```
HTTP request
   ↓
FastAPI router (thin, ≤ 5 lines)
   ↓  Depends(require_scope("<op>"))
core/scope.require_scope   ────►  services/effective_role.effective_role
   ↓ (authorized)
services/<Inventory|RoleAssignmentService|site_service|device_group_service>
   ↓
db/models  ─►  Postgres/SQLite
```

Identical to Phase 3/4 flag-on, but there are no flag branches anywhere and no legacy paths to skip past. The only site-scoping code path is the one in `services/effective_role.py`.

---

## 7. Merge strategy

- **Mergeable to `main`?** ✅ **Yes.** Depends on Phase 4 being deployed in prod and having soaked for at least one release cycle.
- **Blast radius:** any external client still calling deprecated endpoints will now get 404. Confirm via access logs before merging (grep for `/device-groups/*/members`, `/users/*/allowed-sites` in the past 30 days — expected count: 0).
- **PR checklist:**
  - [ ] `backend/migrations/versions/<ts>_msp_cleanup.py`
  - [ ] Model deletions (§3.1)
  - [ ] Service deletions (§3.2–3.4, 3.8)
  - [ ] `authz.py` deletion (§3.5)
  - [ ] Config flag deletion (§3.6)
  - [ ] Route deletions (§3.7)
  - [ ] `grep-audit gate` output attached — must be empty
  - [ ] Diagram regenerated
  - [ ] `CHANGELOG.md` "Removed" section listing every deletion
  - [ ] Access-log confirmation attached (no deprecated-endpoint traffic in the past N days)

---

## 8. Rollback

**Not cleanly reversible.** The pragmatic recovery path is:
1. Revert the Phase-5 PR (restores code).
2. Restore DB from the pre-M4 snapshot (T0.2 procedure — take a fresh snapshot immediately before Phase 5 deploy).

`alembic downgrade` will recreate empty legacy tables but cannot repopulate them without the pre-M4 data.

**Mitigation:** take a full DB snapshot the day of Phase-5 deploy; keep it for at least 30 days. Document snapshot location in the PR body.

---

## 9. Exit criteria

- [ ] Alembic M4 applied in prod.
- [ ] Grep-audit gate returns zero hits.
- [ ] `MSP_STRICT_HIERARCHY` gone from config, code, docker-compose, .env.example.
- [ ] Diagram updated + PDF exported.
- [ ] Test suite green (Phase-1 through Phase-4 tests still run; Phase-5 test added).
- [ ] `CHANGELOG.md` "Removed" section complete.
- [ ] Pre-M4 snapshot documented and retention SLA set.
