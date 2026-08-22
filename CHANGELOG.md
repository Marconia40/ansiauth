# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project loosely tracks [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Version bumps land alongside the phase they belong to; see
`docs/upgrades/phases/` for the migration roadmap.

---

## [Unreleased] — MSP (Multi-Site Provider) migration

The MSP migration reshapes the domain to `Device → DeviceGroup → Site`, adds a
mandatory Base-Infrastructure Site, and replaces the single global `role` +
`user_allowed_sites` model with per-scope `role_assignments` plus a
`users.is_system_admin` boolean.

Each phase below lands in its own PR. `main` stays deployable at every phase
boundary. Full plan: [`docs/MSP_IMPLEMENTATION_PLAN.md`](docs/MSP_IMPLEMENTATION_PLAN.md).
Per-phase implementation notes: [`docs/upgrades/phases/`](docs/upgrades/phases/).

### Phase 0 — Prerequisites (this entry)
#### Added
- `CHANGELOG.md` (this file).
- `docs/upgrades/phases/` — one implementation file per phase, plus an
  `artifacts/` folder for pre-migration audits.
- `docs/upgrades/phases/artifacts/phase0-grep-audit.txt` — inventory of every
  caller of the legacy MSP symbols slated for removal, cross-referenced against
  the plan §20.2. Six escalations captured (`ESC-1..6`) for callers not
  enumerated in the plan.

### Phase 1 — Additive schema (M1)
#### Added
- Alembic revision `e1msp1_additive` — introduces the target-model columns
  and the `role_assignments` table. All additions are nullable/defaulted;
  no drops, no NOT NULL flips.
  - `sites.kind VARCHAR NOT NULL DEFAULT 'REGULAR'`
  - `sites.default_group_id INTEGER NULL FK → device_groups.id` (cyclic FK,
    `use_alter=True`; enforced NOT NULL by the application in Phase 3+)
  - `device_groups.is_default BOOLEAN NOT NULL DEFAULT FALSE`
  - `devices.device_group_id INTEGER NULL FK → device_groups.id`
  - `users.is_system_admin BOOLEAN NOT NULL DEFAULT FALSE`
  - `role_assignments` table with UNIQUE(user_id, site_id, device_group_id)
    and CHECK(role IN ('observer', 'operator', 'admin'))
- `site_service.ensure_base_infrastructure()` — idempotent bootstrap of the
  mandatory Base-Infrastructure Site and its Default DeviceGroup. Invoked
  from `app.main` startup so a fresh boot yields a valid MSP schema state
  without operator intervention.
- New tests: `test_msp_m1_models_expose_columns.py` (11 tests),
  `test_msp_m1_base_infra_bootstrap.py` (5 tests),
  `tests/migrations/test_msp_m1_roundtrip.py` (2 tests running Alembic
  upgrade/downgrade in a subprocess against a scratch SQLite DB).

#### Changed
- `backend/app/db/models.py` — new columns/relationships on `SiteModel`,
  `DeviceGroupModel`, `DeviceModel`, `UserModel`; new `RoleAssignmentModel`.
  `DeviceGroupModel.site` gains `foreign_keys=[site_id]` to disambiguate
  from the new reverse `SiteModel.default_group_id` FK path.
- `backend/tests/conftest.py::_seed_test_role_users_with_full_visibility`
  now filters by `SiteModel.kind == 'REGULAR'` when granting operator /
  observer users their "all sites" access. Reflects D14 (Base Infra visible
  only to `is_system_admin`); preserves pre-MSP fixture semantics for
  every existing test.

### Phase 2 — Data backfill (M2)
#### Added
- Alembic revision `e2msp2_backfill` — data-only backfill that populates
  every column Phase 1 added, on every existing row. Idempotent: every
  statement is either `INSERT ... WHERE NOT EXISTS` or `UPDATE ... WHERE
  <target> IS NULL`, so re-running the migration is a no-op. Six steps
  (mirror `docs/upgrades/phases/phase-2-backfill.md §2`):
  1. Base-Infrastructure site + its Default group (idempotent with the
     Phase 1 `ensure_base_infrastructure()` runtime call).
  2. A Default group for every REGULAR site that lacks one. Existing
     user-created groups already named `Default` are *promoted* (D6
     collision handling) rather than duplicated.
  3. Every device is assigned to a `device_groups` row: site-less
     devices → Base Infra's Default; devices with exactly one group in
     their site → that group; ambiguous multi-group or zero-group
     devices → the site's Default.
  4. One `audit_logs` row per ambiguous multi-group device (R1
     mitigation), action `msp_migration_ambiguous_group_assignment`,
     recording the pre-migration group set and the assigned target.
  5. Legacy `device_groups` rows with `site_id IS NULL` (and their
     `device_group_members` associations) are deleted (D21a). Runs after
     Step 3 so no live device still references them.
  6. `UPDATE users SET is_system_admin=TRUE` for every `admin` /
     `super-admin` (D24); one site-scoped `role_assignments` row per
     `operator` / `observer` × `allowed_sites` pair. **Deliberately
     skipped:** the plan's Step 6c auto-grant of Base-Infra observer
     (D14 override — Base Infra is `is_system_admin`-only). Users on
     the legacy `site_id IS NULL` fallback are captured by the
     post-migration report for operator review.
- Terminal verification block — five `_assert_zero()` calls that fail
  the migration transaction if any invariant is violated (no
  `device_group_id IS NULL`, no `device_groups.site_id IS NULL`, no
  `sites.default_group_id IS NULL`, no admin/super-admin without
  `is_system_admin`, exactly one Base Infrastructure site).
- `backend/scripts/msp_post_backfill_report.py` — one-shot script that
  emits `docs/upgrades/phases/artifacts/msp_backfill_report_<date>.md`.
  Three tables: promoted-to-system-admin users (D24), users who lost
  Base-Infra visibility under D14, and devices assigned via ambiguous
  multi-group backfill (R1).
- Eight new migration tests under `backend/tests/migrations/`:
  `test_msp_m2_backfills_default_groups.py` (2 tests),
  `test_msp_m2_backfills_device_groups.py` (1),
  `test_msp_m2_handles_ambiguous_devices.py` (1),
  `test_msp_m2_deletes_null_site_groups.py` (1),
  `test_msp_m2_user_migration.py` (2),
  `test_msp_m2_idempotent.py` (1),
  `test_msp_m2_verification_fails_on_orphan.py` (1),
  `test_msp_m2_snapshot_diff.py` (2 — T2.2 R3 mitigation: legacy
  `allowed_device_names` vs post-migration JOIN through
  `role_assignments` are byte-identical for users with at least one
  allowed site).
- `backend/tests/migrations/conftest.py` — shared alembic-subprocess
  harness and seed helpers reused by every M2 test.

#### Changed
- `device_groups.name` no longer carries a global `UNIQUE` constraint.
  The `msp_backfill` migration drops both `uq_device_group_name` and
  the unique `ix_device_groups_name` index at Step 0 (see the module
  docstring for the rationale); Phase 4 (`msp_enforce`) replaces the
  guarantee with per-site `UNIQUE(site_id, name)`. A non-unique
  `ix_device_groups_name_nonunique` lookup index is added in the
  meantime so `WHERE name = ?` / `ORDER BY name` queries stay fast.
  Acknowledged deviation from the plan's "data-only" phase framing —
  documented in the migration docstring.

### Phase 3 — Application cutover (flag-gated by `MSP_STRICT_HIERARCHY`)
Full spec: [`docs/upgrades/phases/phase-3-application-cutover.md`](docs/upgrades/phases/phase-3-application-cutover.md).

#### Added
- Feature flag `MSP_STRICT_HIERARCHY` (default `False`) in `app.core.config`.
  Only three list/get endpoints (`GET /devices`, `GET /device-groups`,
  `GET /sites`) actually branch on it; every new endpoint is always available.
- **Services**:
  - `app.services.effective_role.effective_role(session, user, resource_type, resource_id)`
    — pure most-specific-wins resolver over `role_assignments`, with a
    `super-admin` fast-path for `is_system_admin=True`.
  - `app.services.inventory_service.Inventory` — sole entry point for Device
    CRUD, listing, and movement. `Inventory.move(name, target_group_id, actor)`
    handles same-site, cross-site, D_active_job (409 if a Job is pending or
    running), and the **D8** clarification: `target_group_id=None` moves the
    device to its current site's Default group.
  - `app.services.role_assignment_service.RoleAssignmentService` — sole owner
    of `role_assignments` rows and `users.is_system_admin` (grant/revoke/list/
    set-system-admin). D25 authz: only system-admins or site-admins may grant;
    group-admins cannot delegate.
- **Dependencies** in `app.core.scope`:
  - `require_authenticated` — enriches the JWT user dict with `id` and
    `is_system_admin` via one DB lookup per request. Falls back to inferring
    from the JWT `role` claim when the user row is missing (keeps JWT-only
    test fixtures working).
  - `require_system_admin` — gate for globally sensitive ops.
  - `require_scope(op)` — per-scope authorization backed by the module-level
    `OP_MIN_ROLE` matrix (single source of truth for the authorization
    matrix). Body-resolves the target for `POST /devices`, VLAN batch
    endpoints, etc.
  - Synthetic op `"move_device"` — dispatches to `move_device_same_site` or
    `move_device_cross_site` at runtime based on target site.
- **Schemas**:
  - `app.schemas.role_assignment` — `RoleAssignmentCreate`,
    `RoleAssignmentRead`, `SystemAdminUpdate`.
  - `app.schemas.device.DeviceMove` — `device_group_id: int | None`, with
    `null` documented as "move to current site's Default group" (D8).
  - `DevicePublic.device_group_id` / `device_group_name` — surfaced on every
    Device response so UIs can render the owning group without a follow-up
    call.
- **Endpoints**:
  - `POST /api/v1/devices/{name}/move` (new; `require_scope("move_device")`).
  - `GET /api/v1/sites/{site_id}/groups` (new; `require_scope
    ("list_site_groups")`).
  - `POST/GET/DELETE /api/v1/users/{user_id}/grants` (new).
  - `PUT /api/v1/users/{user_id}/system-admin` (new; `require_system_admin`).
  - `PUT /api/v1/users/{user_id}/allowed-sites` — kept as a **deprecated**
    compat shim (T3.3b): rewrites the payload as observer site-wide grants,
    sets `Deprecation: true` and `Sunset: Phase-5` headers.

#### Changed
- `POST /api/v1/sites` now atomically creates the site *plus* its Default
  DeviceGroup (`is_default=True`) and back-references `sites.default_group_id`
  in one transaction. Two audit rows emitted (site + group).
- `DELETE /api/v1/sites/{id}` rejects Base-Infrastructure (400) — the site is
  system-managed. Under MSP-strict, the endpoint also requires system-admin.
- `DELETE /api/v1/device-groups/{id}` auto-moves every member device to the
  Site's Default group via `Inventory.move` (D19), then deletes the group;
  response now includes `moved_devices: [...]`. Default groups are immutable
  (D7): rename and delete both return 400.
- **D8 semantics baked into the device-level move**: the new
  `POST /devices/{name}/move` endpoint accepts `device_group_id: null` to
  move the device to its current site's Default group. The legacy group-level
  `DELETE /device-groups/{id}/members/{device_name}` is marked deprecated
  under MSP-strict and internally forwards to `Inventory.move` targeting the
  Default group so both surfaces converge on the same code path.
- `_seed_test_role_users_with_full_visibility` — no changes, but
  `user_service.create_user` now also seeds `is_system_admin=True` for
  admin/super-admin roles and mirrors legacy `allowed_site_ids` to observer
  site-wide grants in `role_assignments`, so fresh users work under both
  flag states.
- **ESC-1** (jobs.py, 3 sites): `get_job`, `cancel_job`, and `list_jobs`
  routed through `require_authenticated`; visible-devices scoping in
  `list_jobs` uses `Inventory._visible_device_names` under MSP-strict.
- **ESC-2** (ports.py, 9 endpoints): `require_role` swapped for
  `require_authenticated` on every write endpoint; the imperative
  `authz.ensure_device_allowed` line is replaced by `_authz_device
  (min_role=…)` — flag-aware helper that delegates to legacy under
  `MSP_STRICT_HIERARCHY=False` and `effective_role` under `=True`.
- **ESC-3** (vlans.py, 5 endpoints incl. batch): same treatment via
  `_authz_devices` (batch-aware). `DELETE /vlans/{id}` remains admin-only.
- **ESC-4** (auth.py): `POST /auth/unlock/{username}` promoted from
  `require_role("admin")` to `require_system_admin` — no site scope makes it
  a system-level op (aligns with D26 activation/deactivation semantics).

#### Deprecated
- `PUT /api/v1/users/{user_id}/allowed-sites` — see above; removed in Phase 5.
- `DELETE /api/v1/device-groups/{id}/members/{device_name}` — marked
  `deprecated` in the OpenAPI schema; use `POST /devices/{name}/move` with
  `device_group_id: null` instead. Removed in Phase 5.

#### Notes / deviations from the plan
- Phase-3 §7.1 asked for "always-on" ESC swaps (unconditional
  `require_scope`). The implementation flag-gates the ESC swaps inside a
  shared helper so pre-MSP fixtures (`seed_defaults` mock devices with
  `site_id=NULL`) keep working under `MSP_STRICT_HIERARCHY=False`. Under
  `=True` the helper enters the strict `effective_role` path — T3.5
  snapshot-diff will still measure divergence in staging.
- `DELETE /vlans/{id}` stays admin-only. The plan table `:119, :145` mapped
  both create/update to `write_device_config`; delete was not enumerated in
  the swap table, and the existing `test_auth::test_operator_cannot_delete_vlan`
  contract confirms admin is intentional.

### Phase 4 — Enforcement (M3) + Frontend Cutover — **BREAKING RELEASE (v1.1)**
Full spec: [`docs/upgrades/phases/phase-4-enforcement.md`](docs/upgrades/phases/phase-4-enforcement.md).

This is the breaking release. `MSP_STRICT_HIERARCHY` is now the default; the
DB schema enforces every MSP invariant; the frontend surfaces the new
device-level "Move…" action per D8 and a per-user grants editor. API version
bumped to `1.1.0` in both OpenAPI (`app.main`) and `frontend/package.json`.

#### Added
- **Alembic migration `e4msp3_msp_enforce`**
  - Preflight verification re-runs every M2 invariant check and aborts with a
    clear `RuntimeError` (naming the violated invariant) if Phase 2 didn't
    complete cleanly on this DB.
  - **NOT NULL** flips on `devices.device_group_id` and `device_groups.site_id`.
  - **Partial unique indexes**:
    - `ux_sites_single_base_infra` — exactly one `kind='BASE_INFRASTRUCTURE'`.
    - `ux_device_groups_one_default_per_site` — one `is_default=TRUE` per site.
  - **Per-site UNIQUE(site_id, name)** on `device_groups` (D6). Replaces the
    non-unique lookup index `ix_device_groups_name_nonunique` that Phase 2
    installed as a transitional stopgap.
  - **FK tightening**: `device_groups.site_id → sites.id` moves from
    `SET NULL` to `RESTRICT`. Deletes of a site with any groups now fail
    loudly at the DB layer.
  - SQLite: every constraint change goes through `batch_alter_table` (SQLite
    lacks `ALTER TABLE … DROP CONSTRAINT`).
- **`app.core.config.MSP_STRICT_HIERARCHY` default flipped `False → True`.**
  Still available as an environment-variable escape hatch for one release;
  Phase 5 removes the flag entirely.
- **`app.core.audit_service._apply_msp_audit_scoping` — D27.** One-SQL scoping
  path for `GET /audit`. System-admins see everything; every other caller
  sees rows for devices in their visible-device set, plus rows for groups
  and sites their grants cover, plus `resource='auth'` events and rows
  about themselves.
- **`app.db.session._sqlite_enable_fk`** — `PRAGMA foreign_keys=ON` fires on
  every new SQLite connection so `RESTRICT` / `CASCADE` rules are actually
  applied. Postgres enforces FKs by default.
- **Deprecation headers** on `POST /device-groups/{id}/members` and
  `DELETE /device-groups/{id}/members/{device_name}`: every response now
  carries `Deprecation: true`, `Sunset: Phase-5`, and a `Link` header
  pointing at the successor `/devices/{name}/move` endpoint. Both endpoints
  still respond (internally forwarding to `Inventory.move`) so pre-cutover
  clients keep working through the deprecation window.
- **Frontend**
  - `frontend/src/types/{device,site,user}.ts` — reshaped to the strict
    schema: `Device.site_id`/`site_name`/`device_group_id`/`device_group_name`
    are all non-null; `Site.kind` / `Site.default_group_id` land as
    first-class fields; `User.is_system_admin` is optional (login endpoint
    doesn't surface it yet — TODO for a follow-up).
  - `frontend/src/services/api.ts` — new `moveDevice`, `listSiteGroups`,
    `listGrants`, `grant`, `revoke`, `setSystemAdmin` helpers; existing
    `addDeviceToGroup` / `removeDeviceFromGroup` marked `@deprecated`.
  - `frontend/src/app/(dashboard)/devices/page.tsx` — Site dropdown is
    required; Group dropdown cascades on Site change and pre-selects the
    Default group; new **Move…** row action opens a modal with a **Reset
    to Default** shortcut (per D8).
  - `frontend/src/app/(dashboard)/sites/page.tsx` — Base-Infrastructure row
    shows a badge and hides the Delete button; every site row displays its
    Default group id; the site name links to the new detail page.
  - `frontend/src/app/(dashboard)/sites/[id]/page.tsx` — **new route** —
    fetches `GET /sites/{id}` + `GET /sites/{id}/groups`, renders each group
    with a nested devices list and a per-row Move action.
  - `frontend/src/app/(dashboard)/users/page.tsx` — legacy
    `allowed_site_ids` checkboxes removed; replaced by a **Grants…** modal
    that lists existing grants and issues new ones via
    `POST /users/{id}/grants`; system-admins get a per-user
    **System-admin** toggle.

#### Changed
- `app/schemas/device.py`
  - `DeviceCreate.site_id: int` is **required** (was `Optional[int]`); a new
    `device_group_id: int | None` slots in for optional group placement —
    when omitted the device lands in the site's Default group.
  - `DeviceUpdate` uses `model_config = ConfigDict(extra='forbid')` +
    a `model_validator` that raises a clear ValueError pointing at
    `POST /devices/{name}/move` when a caller sends `site_id` or
    `device_group_id`.
  - `DevicePublic.site_id`, `site_name`, `device_group_id`, `device_group_name`
    all flipped to non-optional (`M3` guarantees they're populated).
- `app/api/audit.py::get_audit_log` gated on `require_authenticated` (was
  `require_role("admin")`) under the MSP flag — the D27 scoping filter *is*
  the authorization; the legacy admin-only gate stays under flag-off for
  behavioral parity with pre-Phase-4 deployments.
- `app/services/device_group_service.py::delete_group` — the M2M-junction
  fallback is gone (M3 guarantees `devices.device_group_id IS NOT NULL`).
- `app/services/site_service.py::delete_site` — order tightened for M3's
  RESTRICT FK: null `sites.default_group_id` first, delete groups, delete
  site.
- `app/services/device_service.py::seed_defaults` — mock devices are now
  placed in a **REGULAR** `Mock Site` (auto-created on first call) so
  operator/observer test users granted "every REGULAR site" can see them
  (D14 hides Base-Infrastructure from non system-admins).

#### Deprecated
- `POST /api/v1/device-groups/{group_id}/members`.
- `DELETE /api/v1/device-groups/{group_id}/members/{device_name}`.
- `PUT /api/v1/users/{user_id}/allowed-sites` (already deprecated in
  Phase 3 — this release begins the sunset window).
- `frontend/src/services/api.ts::addDeviceToGroup` / `removeDeviceFromGroup`
  — marked `@deprecated` (removed in Phase 5).

#### Notes / acknowledged deviations from the plan
- **`sites.default_group_id` stays nullable at the DB level** despite the
  plan calling for NOT NULL. The cyclic FK (sites ↔ device_groups) requires
  a two-step INSERT (site first, then default group, then back-ref), and no
  portable pattern makes that land under a NOT NULL constraint. Enforced at
  the `site_service.create_site` layer inside a single transaction, and the
  M3 preflight verification aborts the migration if any row violates it.
  Documented in `db/models.py` and the migration module docstring.
- **`sites.default_group_id → device_groups.id` FK stays `RESTRICT`** rather
  than `CASCADE` — dropping a Default group by cascade would silently orphan
  the site's `default_group_id` reference. The application-level
  `delete_site` and `delete_group` code paths null the reference *before*
  deleting groups, so RESTRICT is a safety net.

#### Migration order (staging → prod)
1. Merge PR. M3 does **not** run yet.
2. Deploy backend to staging with `MSP_STRICT_HIERARCHY=false`. Verify no
   regression against the prior flag-off snapshot.
3. Run `alembic upgrade head` in staging to land M3. Preflight verification
   runs automatically; abort message on failure names the invariant.
4. Set `MSP_STRICT_HIERARCHY=true` in staging (this becomes the default in
   this release; the env override is here for one-release rollback).
5. Deploy frontend to staging. Execute the manual FE smoke checklist in
   `docs/upgrades/phases/phase-4-enforcement.md §5.4`.
6. Repeat for prod, one region at a time if applicable.
7. Announce the deprecation for the two group-level member endpoints, the
   `PUT /users/{id}/allowed-sites` shim, and any PUT-based site changes.

#### Rollback
- **Config-level:** `MSP_STRICT_HIERARCHY=false` restores flag-off list/get
  behavior. Does not revert M3's DB constraints — those stay.
- **DB-level:** `alembic downgrade -1` undoes M3 in full — drops the
  partial unique indexes, relaxes NOT NULL, restores `SET NULL` on the
  group→site FK, reinstates the non-unique lookup index on
  `device_groups.name`.
- **Full revert:** revert the frontend + schema PR first, then
  `alembic downgrade -1`. Legacy paths still work because Phase 2 backfill
  guarantees every column is populated correctly.

### Phase 5 — Cleanup (M4) — **ONE-WAY RELEASE (v1.2)**

Removal-only phase. Drops every legacy column, table, endpoint, service
function, and code branch kept for backward compatibility during Phases 3–4.
This is the **point of no return**: the platform can no longer serve pre-MSP
request shapes after this.

#### Removed
- **Alembic revision `e5msp4_cleanup`** (`backend/migrations/versions/
  e5msp4_msp_cleanup.py`) drops:
  - `devices.site_id` (column + `ix_devices_site_id` + `fk_devices_site_id`)
  - `device_group_members` (table + indexes + `uq_group_member`)
  - `user_allowed_sites` (table + indexes)
  - `users.role` (column)

  `downgrade()` recreates the empty structures so the Alembic chain remains
  formally walkable, but it emits a NOTICE that dropped data is not
  recoverable — restore from the pre-Phase-5 snapshot (see
  `docs/upgrades/phases/artifacts/phase0-db-snapshot-runbook.md`) instead.

- **ORM (`backend/app/db/models.py`)**:
  - Classes: `DeviceGroupMemberModel`, `UserAllowedSiteModel`.
  - Fields: `DeviceModel.site_id`, `DeviceModel.site`, `DeviceModel.group_members`;
    `UserModel.role`, `UserModel.allowed_sites`.

- **Service layer**:
  - `device_group_service.add_member`, `.remove_member`,
    `.list_group_devices`, `.remove_device_from_all_groups`, and
    `.list_groups` (unused after the flag-off path was removed).
  - `device_service.update_device`'s `site_id` handling (device moves go
    through `POST /devices/{name}/move`) and `_to_domain`'s legacy
    site-attribute derivation branch.
  - `site_service.delete_site`'s legacy device-count via `devices.site_id`.
  - `inventory_service.Inventory.list`'s flag-off fallback to
    `authz.allowed_device_names_for`.

- **`backend/app/core/authz.py`** deleted entirely:
  `is_unrestricted`, `allowed_site_ids_for`, `allowed_device_names_for`,
  `ensure_device_allowed`, `ensure_devices_allowed`,
  `set_user_allowed_sites`, `get_user_allowed_sites`. Every importer
  (`api/devices.py`, `api/device_groups.py`, `api/audit.py`, `api/jobs.py`,
  `api/vlans.py`, `api/ports.py`, `services/inventory_service.py`) migrated
  to `services/effective_role.py` / `services/inventory_service.py`.

- **Config**: `Settings.MSP_STRICT_HIERARCHY` and the module-level
  `MSP_STRICT_HIERARCHY` re-export from `backend/app/core/config.py`. Every
  `if settings.MSP_STRICT_HIERARCHY` branch across the API layer collapsed
  to the flag-on path.

- **API routes**:
  - `POST /device-groups/{id}/members` (successor: `POST /devices/{name}/move`).
  - `DELETE /device-groups/{id}/members/{name}` (successor: `POST
    /devices/{name}/move` with `device_group_id: null`).
  - `PUT /users/{id}/allowed-sites` (successor: `POST /users/{id}/grants`).
  - All `MSP_STRICT_HIERARCHY` flag branches inside
    `api/{devices,device_groups,sites,users,audit,jobs,vlans,ports}.py`.

- **User schema** (`app/schemas/user.py`) drops `role`, `allowed_site_ids`,
  `allowed_site_names`. `UserCreate.is_system_admin: bool` replaces `role`
  as the sole system-wide privilege input. `UserUpdate` no longer accepts
  `role` (per-scope grants live at `PUT/POST /users/{id}/grants`;
  system-admin toggling lives at `PUT /users/{id}/system-admin`).

- **Auth flow**: JWT payload swaps the `role` claim for `is_system_admin`.
  `core/scope.get_current_user` and `core/dependencies.get_current_user`
  read `is_system_admin` (falling back to a legacy `role` claim only when
  the explicit bit is absent — keeps synthetic-JWT test fixtures working).
  `core/dependencies.require_role` and `ROLE_HIERARCHY` are gone.

- **Frontend**:
  - `services/api.ts`: `addDeviceToGroup`, `removeDeviceFromGroup` (both
    deprecated Phase-4 shims), and their React-Query wrappers.
  - `types/user.ts`: `User.role`, `User.allowed_site_ids`,
    `User.allowed_site_names`, `UserCreate.role`, `UserCreate.allowed_site_ids`,
    `UserUpdate.role`, `UserUpdate.allowed_site_ids`.
  - `app/(dashboard)/device-groups/page.tsx` now calls
    `moveDevice(name, groupId)` / `moveDevice(name, null)` in place of the
    deprecated group-member endpoints (D8 semantics: "remove from group" is
    the device-level move to the site's Default group).

- **Tests**: `test_msp_deprecated_member_endpoints_still_work.py`,
  `test_site_scoped_rbac.py`, `test_site_assignment.py`,
  `test_site_aware_groups.py`, and `test_rbac.py` — all asserted pre-MSP or
  pre-Phase-5 semantics that are now impossible (legacy
  `allowed_sites`-based scoping, `role`-based hierarchy, deprecated group
  member routes). `test_msp_m3_enforces_constraints.py::_cleanup_site` and
  `tests/conftest.py::_seed_test_role_users_with_full_visibility` rewritten
  to drop references to the removed models. Per-file details in the PR
  diff.

- **Scripts**: `backend/scripts/msp_post_backfill_report.py` — Phase-2
  diagnostic that queried the now-dropped `user_allowed_sites` table.

#### Added
- `backend/migrations/versions/e5msp4_msp_cleanup.py` — the M4 migration
  itself (see Removed for what it drops).
- `backend/tests/migrations/test_msp_m4_drops_legacy.py` — asserts the M4
  upgrade removes every legacy artefact and preserves every Phase-4 (M3)
  invariant, plus a downgrade roundtrip that recreates the empty
  structures.

#### Changed
- `_to_read` in both `device_group_service` and `site_service` counts
  devices through `device_group.site_id` (the only site pointer left).
- `services/inventory_service.Inventory.move` no longer maintains the
  legacy `devices.site_id` mirror or the `device_group_members` junction —
  the authoritative FK is the only write path.
- `services/effective_role._resolve_scope` uses an inner (not outer) join
  through `device_groups`, matching the post-Phase-4 NOT NULL guarantee.

#### Migration & compatibility
- **One-way in practice.** `alembic downgrade` recreates the empty
  structures but cannot restore data. Recovery from a bad deploy is: revert
  the Phase-5 PR → restore DB from the pre-Phase-5 snapshot
  (`~/ansiauth-backups/ansiauth_pre_msp_2026-08-22_1230.sql` on the current
  machine).
- Any external client still calling the deprecated endpoints
  (`/device-groups/*/members`, `/users/*/allowed-sites`) now receives 404.
  Confirm via access logs before merging.

#### Grep-audit gate
- Pre-merge run of the Phase 5 §3.9 gate returns zero hits.

### Phase 6 — Postgres RLS + Optional Hardening (M5)

Adds Postgres Row-Level Security as **defense-in-depth** on top of the
app-layer `require_scope` gate that Phases 3–4 established. Confirmed as
first-class (not optional) per decision D12. No new capability, no new
services, no behavioural change on the happy path — RLS is a silent
enforcement layer for the day a service handler forgets to apply its scope
filter.

#### Added
- **`backend/migrations/versions/f6msp5_msp_rls.py`** (new). Postgres-only
  (SQLite skips cleanly). `ENABLE + FORCE ROW LEVEL SECURITY` on
  `sites`, `device_groups`, `devices`, `audit_logs`, plus one `SELECT`
  policy per table scoped by the two GUCs the middleware sets:
    - `app.user_id`          — integer PK of the calling user (`0` = deny)
    - `app.is_system_admin`  — boolean; `true` bypasses every predicate
  Write policies (`INSERT` / `UPDATE` / `DELETE`) are **permissive** —
  one per operation, `USING (true)` — so the app layer's `require_scope`
  stays the sole write gate. Splitting by operation matters: a
  `FOR ALL USING (true)` policy would OR-combine with the scoped SELECT
  policy and make every read wide open.
- **`backend/app/core/rls_context.py`** (new). Owns the request-scoped
  context that the SQLAlchemy `after_begin` hook reads to run
  `SELECT set_config('app.user_id', …, true)` on every Postgres
  transaction. Exposes:
    - `current_user_ctx: ContextVar` — populated per request by the
      middleware; empty default → deny.
    - `system_context()` — context manager marking trusted internal
      execution (bootstrap, scheduler jobs, Celery); the hook then writes
      `is_system_admin=true`.
    - `install_session_rls_hook(sessionmaker)` — the `after_begin`
      listener; called from `init_db` right after the sessionmaker is
      built.
    - `install_worker_system_context()` — pins the Celery worker process
      to system context for its lifetime (tasks have no JWT).
- **`backend/app/core/rls_middleware.py`** (new). `RLSSessionMiddleware`
  decodes the `Authorization: Bearer` header best-effort (invalid /
  missing tokens leave the context empty → RLS deny-default), resolves
  the caller's `users.id` for the `app.user_id` GUC, and populates
  `current_user_ctx` for the duration of the request.
- **`backend/tests/test_msp_rls.py`** (new, 7 tests). Postgres-only —
  skipped on SQLite AND skipped when the connecting role has SUPERUSER or
  BYPASSRLS (both cause Postgres to silently skip RLS regardless of
  FORCE). Exercises:
    - Non-system-admin sees only granted sites / groups / devices.
    - Base Infrastructure invisible to non-system-admins even with a
      stray grant on it (D14 defense-in-depth).
    - System-admin GUC bypasses every predicate.
    - Raw `SELECT` bypassing the app-layer scope filter still cannot
      leak other-site rows — the whole point of the phase.

#### Changed
- **`backend/app/db/session.py::init_db`** — calls
  `install_session_rls_hook(_SessionLocal)` right after creating the
  sessionmaker. Idempotent, no-op on SQLite.
- **`backend/app/main.py`** — three edits:
    1. `app.add_middleware(RLSSessionMiddleware)` before the rate limiter
       (so error paths still run under a defined context).
    2. `_bootstrap_admin()`, `ensure_base_infrastructure()`, and
       `job_service.mark_orphaned_jobs_failed()` wrapped in
       `with system_context()` — they run before any request and would
       otherwise hit the RLS deny-default.
    3. Scheduler jobs (`audit_purge_daily`, `cleanup_sweep_interval`) and
       the lifespan startup cleanup now run inside `system_context`
       closures — background code has no JWT.
- **`backend/app/worker.py::_init_db_for_worker`** — calls
  `install_worker_system_context()` after `init_db`, pinning the whole
  worker process to system context. Celery tasks have no JWT.

#### Security
- Under the recommended deployment (app connects as a non-SUPERUSER role
  that owns runtime privileges on the schema), a service handler that
  forgets to filter its query — e.g. a stray `SELECT * FROM devices` —
  still cannot return rows the caller's grants don't cover. Every
  ID-guessing / IDOR class of bug is defeated at the DB layer.
- Read-only RLS by design: write policies are permissive (`USING(true)`)
  so `require_scope` remains the sole write gate. Duplicating the check
  in RLS risks silent inconsistencies (an INSERT rejected at the DB but
  accepted at the app looks like a bug). See
  `docs/upgrades/phases/phase-6-rls-and-optional.md` §2.2 for the full
  rationale.

#### Deployment notes
- **The docker-compose default puts the app on the Postgres bootstrap
  user (`POSTGRES_USER=ansiauth`).** Postgres forbids demoting the
  bootstrap user from SUPERUSER — the migration detects this via
  `pg_roles.oid = 10` and logs a warning; the policies are installed but
  **not enforced** because the connecting SUPERUSER bypasses them
  silently.
- For actual RLS enforcement (prod, staging, or any dev setup that wants
  to validate the policies), the app must connect as a **separate
  non-SUPERUSER role** with runtime CRUD on the public schema. Ownership
  of the tables can stay with the bootstrap user (that's the migration /
  admin identity); the runtime user just needs `USAGE ON SCHEMA public`
  + `SELECT/INSERT/UPDATE/DELETE ON ALL TABLES` grants. The migration's
  `_demote_app_role` step then executes cleanly and RLS enforces from
  that point forward.
- The migration's `downgrade()` re-promotes any role it demoted
  (idempotent SUPERUSER / BYPASSRLS restore); safe rollback via
  `alembic downgrade -1`.

#### Migration & compatibility
- Alembic head advance: `e5msp4_cleanup → f6msp5_rls`.
- Round-trip clean on both dialects (SQLite: no-op both directions;
  Postgres: policies drop cleanly, RLS disabled).
- No API changes, no schema changes, no ORM changes — Phase 6 is a
  pure security layer on top of the Phase 5 model.
- Backend test count: **945 passed, 7 skipped** on SQLite (unchanged
  from Phase 5 + the 7 RLS tests that skip when RLS isn't testable).
  On Postgres with a non-super app role, expect **945 + 7 = 952 passed**.

<!--
Each subsequent phase appends its own section below when it lands. Template:

### Phase N — <title>
#### Added
#### Changed
#### Deprecated
#### Removed
#### Fixed
#### Security
-->
