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
