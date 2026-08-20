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
