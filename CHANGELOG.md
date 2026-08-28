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
