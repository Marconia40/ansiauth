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
