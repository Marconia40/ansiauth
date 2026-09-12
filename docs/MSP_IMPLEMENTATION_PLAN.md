# MSP (Multi-Site Provider) Implementation Plan

> **Status:** Analysis and design only. **No code has been modified.**
> **Prompt of origin:** `docs/prompts/new/MSP-Structure.md`.
> **Reference diagram:** `docs/prompts/new/diagrama_clases_dominio.drawio` (+ PDF export).
> **Author role:** produced as a senior-architect analysis of the repository as of
> branch `refactor/MSP-structure`, HEAD `4aad1d7`.

---

## Reading guide

- **Sections 1–8** describe the **current** state (as it is in code today).
- **Sections 9–18** describe the **target** state and how the two differ.
- **Sections 19–28** describe **the work** (per layer) and how existing data survives it.
- **Sections 29–30** capture **risks** and **decisions the user must confirm before implementation begins** — several of them contradict the *previous* internal plan in `docs/prompts/new/plan-migracion-modelo-dominio.md`, which was not merged.
- **Sections 31–33** give an **actionable, phased task breakdown**.

Nothing in Sections 19–33 should be started until the questions in Section 30 are resolved.

---

# 1. Executive Summary

The repository already has the three MSP domain nouns — `Site`, `DeviceGroup`, `Device` — and site-scoped authorization. But the current model **does not enforce the MSP hierarchy**, and its authorization model **cannot express "admin on one Site, observer on another"**:

- `DeviceModel.site_id` is a **nullable direct FK** (`backend/app/db/models.py:70-75`). A Device belongs to a Site *directly*, not through its Group.
- A Device can be a member of **multiple** DeviceGroups simultaneously via `DeviceGroupMemberModel` (`db/models.py:230-247`). Nothing enforces "exactly one Group per Device".
- A Device can exist with **no** DeviceGroup at all (the M2M table is empty for it) and still be operable.
- A `DeviceGroupModel.site_id` is **nullable** (`db/models.py:179-184`); a "legacy" group with no site is a permitted state, hidden from non-admins by the API.
- `Site` creation does **not** produce a Default Device Group (`services/site_service.py:34-43` — it only inserts the Site row).
- **No "Base Infrastructure" concept** exists anywhere in schema, service, or seed data.
- Authorization is enforced by **post-fetch Python filtering**, not by SQL predicates (`api/devices.py:33-38`, `api/device_groups.py:44-50`). A forgotten filter is a silent leak.
- Every user has **one global `role`** (`db/models.py:16`) that applies wherever they have visibility. The system cannot express "admin here, observer there," nor grant a specific Group at a different role than the surrounding Site.

The MSP prompt (`docs/prompts/new/MSP-Structure.md`) requires the opposite of most of the above: **Device → exactly one Group → exactly one Site**, with a **mandatory Default Group per Site**, a **mandatory Base-Infrastructure Site**. On top of that, the user has confirmed (design conversation, 2026-08-18/19) that authorization must be **per-scope**: a user like Juan holds `admin on Site A`, `observer on Site B` (with `operator on B/Group-2` elevating), and `admin on C/Group-1 + C/Group-2` (Groups 3 and 4 invisible). Grants are additive and resolve by max-role per scope (see D11).

**Key strategic call-out.** There is a **prior, unimplemented** design in `docs/prompts/new/plan-migracion-modelo-dominio.md` §7 that proposes moving `Device ↔ Site` to **many-to-many** (a device can live in multiple sites, "most permissive wins"), and adding a direct `user_allowed_devices` grant. **That prior design is superseded** — the MSP prompt is stricter (one Site per Device, via one Group), and the confirmed per-scope role model provides a cleaner, more auditable answer to the "which user can operate on which resource" question. See **Decision D0** in Section 30 for the explicit resolution.

The remainder of the plan is structured to (a) preserve everything the platform does well (vendor driver split, orchestration runner, Celery+Redis dispatch, immutable audit, mock-mode testability); (b) enforce the MSP invariants in the *domain*, not only in the UI (Sections 10–17); and (c) replace the current single-role authorization with per-scope `role_assignments` + a single `is_system_admin` bit (Sections 10.5, 11.5, 12).

---

# 2. Current Architecture

Two-process runtime (`docker-compose.yml`):

- `backend` — FastAPI/uvicorn, runs Alembic on start.
- `worker` — Celery worker (`backend/app/worker.py`), Redis broker.
- `db` — Postgres 16 (SQLite in dev).
- `redis` — broker + `device_lock:{name}` keys + rate-limit slots.

Layer split (`backend/app/`):

| Layer | Path | Purpose |
|---|---|---|
| API routers | `app/api/` | One file per resource: `devices.py`, `device_groups.py`, `sites.py`, `users.py`, `vlans.py`, `ports.py`, `jobs.py`, `group_jobs.py`, `audit.py`, `auth.py`, `health.py` |
| AuthZ | `app/core/authz.py` | Site-scoped filters, `ensure_device_allowed`, `set_user_allowed_sites` |
| RBAC gates | `app/core/dependencies.py` | `require_role(minimum_role)` — hierarchy `observer < operator < admin < super-admin` |
| Domain dataclasses | `app/models/` | `Device`, `Job`, `GroupJob`, `VLANInfo`, `PortInfo`, `PortConfigRequest` |
| Pydantic DTOs | `app/schemas/` | Request/response schemas |
| Services (business logic) | `app/services/` | Free functions, no aggregates: `device_service`, `device_group_service`, `site_service`, `user_service`, `audit_service`, `job_service`, `group_job_service`, `orchestration_runner`, `vlan_execution_service`, `port_execution_service`, `device_locks`, `rate_limiter`, `retry_policy`, `secret_service` |
| Vendor drivers | `app/services/vendors/{cisco,huawei}/` | `port_driver.py`, `vlan_driver.py`, dispatched via `vendors/dispatcher.py` |
| Persistence | `app/db/` | `models.py` (all SQLAlchemy models), `session.py`, `audit_guard.py` (immutability listener) |
| Migrations | `backend/migrations/versions/` | Alembic |
| Ansible | `backend/ansible/project/vendors/{cisco,huawei}/*.yml` | Vendor playbooks |
| Frontend | `frontend/src/app/(dashboard)/` | Next.js 16, flat routes: `/devices`, `/device-groups`, `/sites`, `/users`, `/vlans`, `/ports`, `/jobs`, `/audit` |

For the fuller current-state narrative (execution-flow ASCII, ranked pain points), see `docs/upgrades/02-current-state.md`. This plan does not repeat it.

---

# 3. Current Domain Model

Two parallel layers:

**SQLAlchemy ORM** (`backend/app/db/models.py`):

| Entity | Key columns | Relationships |
|---|---|---|
| `UserModel` | `id`, `username` (unique), `hashed_password`, `role` (observer/operator/admin/super-admin), `is_active` | M2M `allowed_sites` → `SiteModel` via `user_allowed_sites`. **Target: replace `role` with `is_system_admin: bool` and `allowed_sites` with `role_assignments` — see §10.5, §11.5, §24.** |
| `SiteModel` | `id`, `name` (unique), `description` | 1:N `devices`, 1:N `device_groups`, M2M `allowed_users` |
| `DeviceModel` | `id`, `name` (unique), `host`, `vendor`, `platform`, `username`, `encrypted_password`, **`site_id` (nullable FK)** | N:1 `site`, 1:N `group_members` |
| `DeviceGroupModel` | `id`, `name` (unique), `description`, **`site_id` (nullable FK)** | N:1 `site`, 1:N `members` |
| `DeviceGroupMemberModel` | `id`, `group_id` FK, `device_name` FK, `UniqueConstraint(group_id, device_name)` | N:1 `group`, N:1 `device` |
| `UserAllowedSiteModel` | `(user_id, site_id)` composite PK | Junction |
| `JobModel`, `GroupJobModel`, `AuditLogModel`, `RefreshTokenModel`, `LoginAttemptModel` | (Async execution / auth infra — out of scope for MSP domain but tenant-scoping still applies to them) |

**Dataclass domain layer** (`backend/app/models/`):
- `Device` — pure data, no methods; carries `site_id` and `site_name` for convenience.
- `Job`, `GroupJob`, `DeviceExecution`, `VLANInfo`, `PortInfo`, `PortConfigRequest`.
- No `DeviceGroup` or `Site` dataclass — the Pydantic `DeviceGroupRead`/`SiteRead` schemas serve as the read model.

**The model is anemic.** All business rules live as free functions in `services/`. There is no `Device.move_to_group(...)`, no `Site.create_default_group()`, no `DeviceGroup.assert_can_accept(device)`. The two site invariants that *do* exist (`device_service.update_device` and `device_group_service.add_member`) are enforced imperatively in the service layer.

---

# 4. Current Database Model

Migrations, in dependency order (`backend/migrations/versions/`):

1. `d17f392c1e6b_initial_schema` — `audit_logs`, `devices`, `jobs`.
2. `9a675eb5f358_add_users_table`.
3. `c3a9b2e1f4d7_add_refresh_tokens_and_login_attempts`.
4. `e7f4a2b9c810_add_device_groups` — `device_groups` + `device_group_members` (**site-less at creation**).
5. `8b5d2c7a3e1f_add_sites` — `sites` + nullable `devices.site_id` FK (ondelete SET NULL).
6. `9e2a4c8b6f10_add_user_allowed_sites` — `user_allowed_sites` M2M; backfills non-admin users with access to all existing sites.
7. `a3c7e9d1f482_device_group_site_id` — adds `device_groups.site_id` nullable FK; backfills groups whose members all live in the same site.
8. `b8e3a5c1d942_add_group_jobs_and_jobs_columns`.
9. `c4f7e2a9b610_audit_log_immutability_trigger` + `d8a5f2c1b630_drop_sqlite_audit_trigger` — audit trigger portability.

**Data-integrity observations:**

- Devices without a site (`devices.site_id IS NULL`) are legal.
- Device Groups without a site (`device_groups.site_id IS NULL`) are legal (backfill only assigned a site to groups whose members were *all* in the same site).
- A Device may belong to zero, one, or many Groups (unbounded, only `UniqueConstraint(group_id, device_name)` prevents dupes within one group).
- The DB has **no CHECK/trigger** enforcing that `device_group_members.device.site_id == device_group.site_id`. That invariant is app-enforced (`device_group_service.add_member`, `device_service.update_device`).
- Cascade rules: `DeviceGroupMemberModel.device` uses `ondelete="CASCADE"` (defined in the migration, `e7f4a2b9c810` lines 39-40) and the ORM `DeviceModel.group_members` uses `cascade="all, delete-orphan"` — so deleting a device removes it from all groups. Same for delete-group → delete memberships.
- `sites.name`, `device_groups.name`, `devices.name`, `users.username` are all **globally unique** (no tenant column exists).

---

# 5. Current Authorization Model

RBAC (role hierarchy) + Site-scoped filtering.

**Roles** (`core/dependencies.py`, numeric hierarchy):
```
observer=1  <  operator=2  <  admin=3  <  super-admin=4
```

**Unrestricted roles** (bypass all site scoping): `admin`, `super-admin` (`authz.is_unrestricted`).

**Restricted roles** (`observer`, `operator`) filter by `user_allowed_sites`:

- `authz.allowed_site_ids_for(user)` → `None` if unrestricted, else a set (possibly empty).
- `authz.allowed_device_names_for(user)` — the key function:
  - If **non-empty** allowed_sites → devices with `site_id IN (allowed_sites)`.
  - If **empty** allowed_sites → devices with `site_id IS NULL` (fresh-install migration hatch, documented in the docstring lines 60-66).
- `authz.ensure_device_allowed(user, name)` — raises 403 if not in the set.

**Where authorization runs today** (post-fetch, in Python):
- `api/devices.py:33-38` — list, then in-memory filter.
- `api/devices.py:47-50` — get-by-name, then `ensure_device_allowed`.
- `api/device_groups.py:44-50` — list, then filter by `site_id in allowed_sites` (legacy null-site groups hidden from non-admins).
- `api/device_groups.py:59-67`, `:139-143` — get-group / list-devices-in-group, per-request check.
- `api/sites.py:40-42` — **sites list is not filtered** at all (all users see all site names). Only downstream device/group filtering restricts what they can *do*.

**What is NOT enforced anywhere:**
- No DB-level tenant/site predicate. No Postgres Row-Level Security.
- No `tenant_id` or `site_ids` in the JWT (`api/auth.py`).
- No Device-Group-level permissions (only Site-level).
- **No per-scope roles.** A user has *one* global `role` that applies everywhere they have visibility. The system cannot express "admin on Site A, observer on Site B" or "operator on this Group only, invisible everywhere else." Requiring this is the biggest single reshape in the plan — see §10.5, §11.5, §12.

---

# 6. Current Device Lifecycle

**Register:**
```
POST /api/v1/devices  {name, host, vendor, platform, username, password, site_id?}
  → device_service.create_device
    → validates vendor ∈ {cisco_ios, cisco, huawei}
    → encrypts password (AES-256, secret_service)
    → validates site_id exists (if provided)
    → INSERT devices (site_id may be NULL)
  → audit_service.log_action("create_device")
```
No group assignment happens at creation. The device exists standalone.

**Assign to Group** (separate call):
```
POST /api/v1/device-groups/{group_id}/members  {device_name}
  → device_group_service.add_member
    → asserts group.site_id IS NOT NULL (rejects legacy groups)
    → asserts device.site_id == group.site_id  ← Step 7.4 invariant
    → INSERT device_group_members
```

**Change Site:**
```
PUT /api/v1/devices/{name}  {site_id: <new>}
  → device_service.update_device
    → validates new site exists
    → queries all groups the device is in; if any has a different site_id, REJECT
      "Cannot change site: device is a member of group(s) tied to a different site — remove it first"
    → UPDATE devices SET site_id = <new>
```

**Delete Device:** removes device row; ORM cascades remove all group memberships.

**Delete Site:** `SiteHasDevicesError` (409) if any device has this `site_id`. Devices are never cascade-deleted.

---

# 7. Current Site / Device Group Model

Site:
- Simple entity: `id`, `name`, `description`, timestamps.
- `SiteRead` schema exposes `device_count` (computed on read).
- Creation (`site_service.create_site`) is a single INSERT — **no side effects**, no Default Group is created.

DeviceGroup:
- `id`, `name`, `description`, `site_id` (nullable), `created_at`.
- Since migration `a3c7e9d1f482`, new groups require `site_id` (validated in `create_group` and required by `DeviceGroupCreate` schema `site_id: int = Field(..., ge=1)`).
- Old groups (created before that migration, or bulk-imported) may still have `site_id IS NULL`; the API explicitly hides them from non-admins and blocks member additions.
- There is **no `is_default` flag**. There is no notion of a group per Site being "the default one".
- Membership is many-to-many (Device ↔ DeviceGroupMemberModel ↔ DeviceGroup) via a junction table.

Effectively: today, a **Device with `site_id=X` can be in zero or many Groups within Site X**. This is *less* restrictive than the MSP prompt (which requires exactly one Group per Device) and *equally* restrictive on Site (one Site per Device).

---

# 8. Diagram Analysis

The reference diagram `docs/prompts/new/diagrama_clases_dominio.drawio` (26 classes, PDF exported) shows:

- `Device` with attributes `{name PK, host, vendor, platform, encrypted_password, site_id (FK)}`.
- `DeviceGroup` with `{id PK, nombre, site_id (FK)}` and methods `agregar_device`, `quitar_device`, etc.
- `Site` with `{id PK, nombre, descripcion}`.
- `Inventory` as a service class.
- `Usuario` with `{id, username PK, hashed_password, role, allowed_sites int[]}`.
- Two dashed lines from `Usuario` to `Device`: one labeled "accede a — vía site (unión, más permisivo gana)" and another (orange) "acceso directo".
- A note between Device and Site: "0..* pertenece a (device_sites, antes 0..1)" — indicating a **many-to-many** revision.

**Discrepancies vs. code (as of `4aad1d7`):**

| # | Discrepancy | Classification |
|---|---|---|
| D-A | Diagram shows **`Device`↔`Site` many-to-many** ("device_sites" table). Code has **single FK `devices.site_id`**. | **Architectural inconsistency** — the diagram reflects the *proposed* model in `plan-migracion-modelo-dominio.md` §7 which was never implemented. |
| D-B | Diagram shows `Usuario` with a *direct* access-to-`Device` relation (orange). Code has **no `user_allowed_devices` table**. | **Diagram outdated / proposal never landed.** |
| D-C | Diagram does **not** show `DeviceGroupMemberModel` (the M2M junction). It draws `DeviceGroup ◇→ Device` as an aggregation. | **Architectural simplification in the diagram** — real code uses a junction table. Both are legitimate at the class-diagram level. |
| D-D | Diagram shows `Device.site_id` as an attribute on Device. MSP prompt §6 wants Device→Site via Group (i.e., **no** `site_id` column on `devices`). | **Diagram matches current code, not the MSP target.** |
| D-E | Diagram shows no "Base Infrastructure" or "Default Group" classes. MSP prompt §3, §15 require both. | **Diagram outdated** vs. MSP target. |
| D-F | Diagram shows `Usuario.allowed_sites` as an integer array attribute; code implements it as a junction table `user_allowed_sites`. | **Diagram is a conceptual shorthand; code is the correct persistence.** Not a real conflict. |
| D-G | Diagram shows `Inventory` with `list_devices(site_id)`; code lists all devices and filters by site in Python. | **Diagram-vs-code gap** — the current API doesn't take a `site_id` filter (`/api/v1/devices` returns all visible devices). |

**Conclusion:** the diagram is a snapshot of the *previous proposal*, not of current code and not of the MSP target. Sections 10–11 of this plan supersede it. When Phase 5 lands, the diagram must be regenerated to reflect the model in §10 (not the model in the current diagram).

---

# 9. Gaps Between Current and Target Architecture

Numbered so later sections can reference them.

| # | Gap | Current | Target (MSP prompt) |
|---|---|---|---|
| G1 | Device→Site path | `Device.site_id` (nullable direct FK) | Derived: `Device.device_group.site` (Device has no direct `site_id`) |
| G2 | Device→Group cardinality | 0..N (M2M via `device_group_members`) | Exactly 1 (Device has `device_group_id NOT NULL`) |
| G3 | Group→Site cardinality | 0..1 (nullable) | Exactly 1 (`site_id NOT NULL`) |
| G4 | Default Group per Site | Does not exist | Auto-created on `Site` creation; `Site.default_group_id` |
| G5 | Base Infrastructure | Not modeled | A special mandatory Site (single instance, undeletable) |
| G6 | Device creation flow | Site is optional, no Group assignment | Site-first: pick Site → Group list scoped to Site → default to Default Group |
| G7 | Device movement | `PUT /devices/{name}` toggling `site_id`, plus separate group-member API | Single "move Device to Group X" call; Site changes as a consequence |
| G8 | Global Device Inventory | List all devices, filter by permission in Python | Same UX, but backed by a *query predicate* (SQL scope), not post-fetch filter |
| G9 | Per-scope roles | Global `UserModel.role` applies uniformly wherever the user has visibility | Required: `role_assignments(user, site, group?, role)` table. Different roles on different Sites; different roles on different Groups within a Site. Most-specific wins. `is_system_admin` bool on the user row for the only truly global privilege. (Was "optional" in the first draft of this plan; user confirmed it is a first-class requirement.) |
| G10 | Site-list scoping | `GET /sites` returns everything to observers | Restricted users must not see Sites outside their allowed set |
| G11 | Domain invariants | Enforced imperatively in service functions | Enforced at DB (FK, NOT NULL, CHECK), ORM (relationship constraints), and domain (methods/aggregates) |
| G12 | Default-Group deletability | N/A | Default Group cannot be deleted while its Site exists |
| G13 | Multi-vendor + async job path | Works today | Must not regress — Celery, retry, rollback, audit stay intact |

Gaps G1–G7 are **domain-model changes**. G8–G12 are **enforcement/UX changes**. G13 is a **non-regression constraint**.

---

# 10. Target Domain Model

Object-oriented target model, expressed with responsibility/identity/invariants for each class. Kept **minimal** — do not introduce classes that add no invariant or state. See Section 30 for open decisions that could alter these.

## 10.1 `Site` (Entity, aggregate root for its Groups)
- **Identity:** `id` (surrogate int) or `slug` (see D3).
- **Attributes:** `id`, `name` (unique), `description`, `kind: SiteKind`, `default_group_id` (FK back to `DeviceGroup.id`, nullable **only during transactional creation** — see D4), `created_at`, `updated_at`.
- **`SiteKind`** value: `BASE_INFRASTRUCTURE | REGULAR`. Exactly one row with `kind = BASE_INFRASTRUCTURE` may exist (enforced by partial unique index — see §11.6).
- **Invariants:**
  - Post-commit, every `Site` has a Default `DeviceGroup`.
  - `Site` with `kind = BASE_INFRASTRUCTURE` cannot be deleted.
  - Deletion of a regular `Site` requires that its Default Group have zero devices (all other groups must also be empty, but see D5 for the exact cascade semantics).
- **Ops:**
  - `Site.create(name, description) -> Site` (application service): atomic; INSERTs the site *and* its Default Group.
  - `Site.default_group -> DeviceGroup`.
  - `Site.groups -> Iterable[DeviceGroup]`.

## 10.2 `DeviceGroup` (Entity)
- **Identity:** `id`.
- **Attributes:** `id`, `name` (unique-per-site or globally unique — see D6), `description`, `site_id NOT NULL`, `is_default: bool`, `created_at`.
- **Invariants:**
  - `site_id` is mandatory and immutable after creation.
  - Exactly one Group per Site has `is_default = True` (partial unique index).
  - The Default Group cannot be deleted while its Site exists.
  - The Default Group may or may not be renameable (see D7).
- **Ops:**
  - `DeviceGroup.add_device(device)` — asserts group's `site_id`; sets device's group.
  - `DeviceGroup.remove_device(device)` — moves it to the Site's Default Group (see D8: "remove" semantics).

## 10.3 `Device` (Entity, part of DeviceGroup)
- **Identity:** `id` (surrogate), plus a **`uniqueness_key`** (see §14 — Device Identity Strategy). Today's `name` is retained for backward compatibility as a display / auth alias.
- **Attributes:** `id`, `name`, `host`, `vendor`, `platform`, `username`, `encrypted_password`, **`device_group_id NOT NULL`** (replaces `site_id`), `created_at`.
- **`Device.site` is *derived* through `Device.device_group.site`** — not stored on the row.
- **Invariants:**
  - Exactly one `device_group_id`; NOT NULL; cannot be orphaned.
  - Global uniqueness on the `uniqueness_key` (see §14).
  - `name` remains globally unique (keeps the current UX; deprecation optional later — D9).
- **Ops:**
  - `Device.move_to(group)` — atomic; validates the target group exists and (if scoped auth) that the caller has permission on both source and destination.

## 10.4 `Inventory` (Application Service)
- **Responsibility:** the single entry point for Device CRUD and lookup.
- **Ops:**
  - `Inventory.register(name, host, vendor, ..., site_id, device_group_id=None) -> Device` — if `device_group_id` is `None`, resolves to `site.default_group.id`.
  - `Inventory.deregister(name) -> None`.
  - `Inventory.get(name) -> Device`.
  - `Inventory.list(user) -> Iterable[Device]` — filter is a *query predicate* joining through `role_assignments` (§12.4), not a post-fetch loop.
  - `Inventory.move(device_name, target_group_id, actor) -> Device`.

## 10.5 `User`, `RoleAssignment`, `EffectiveRole` (Entities / Value Objects)

Users are **global rows** — one `users` row per person, unique username. What varies across the system is *which resources* the user can touch and *with what role*. A user can hold `admin` on Site A, `observer` on Site B, and `operator` on a single group within Site B, all at once. There is no single "role" that applies everywhere.

### `User`
- Attributes: `id`, `username`, `email`, `hashed_password`, `is_active`, `is_system_admin: bool` (new — replaces the old string `role` column). Timestamps unchanged.
- **`is_system_admin`** is the *only* system-wide privilege bit. It is not scope-assignable. Only another system-admin can grant it. It's what today's `super-admin` role becomes.
- The old `UserModel.role` string column is dropped. Its values migrate as:
  - `super-admin` → `is_system_admin = TRUE`.
  - `admin` → `is_system_admin = TRUE` (see D24: today's `admin` is functionally unrestricted per `authz.is_unrestricted`; the safest migration is to preserve that).
  - `operator` / `observer` → `is_system_admin = FALSE`; per-Site `role_assignments` rows created from `user_allowed_sites` carrying their existing role name.

### `RoleAssignment` (new entity, backs the `role_assignments` table — see §11.5)
- Represents one grant: `(user, site, [group?], role)`.
- `role ∈ {observer, operator, admin}`. `super-admin` is **not** a scope-assignable role; it lives on the User row as `is_system_admin`.
- If `device_group_id` is NULL, the grant applies to the whole Site (every current and future Group in it).
- If `device_group_id` is set, the grant applies to just that Group. The row also carries `site_id` (= the Group's Site) for query efficiency, but the Group is the authoritative scope.
- Grants are **additive** — multiple grants coexist and the union of their scopes defines what the user can see.
- When two grants overlap on the same resource (a Site grant and a Group grant within it, or a Group grant nested under a Site grant), the **maximum** role of the applicable grants wins for the resource being accessed. Grants only elevate; they cannot downgrade. See `EffectiveRole` below and D11 for the full rationale.

### `EffectiveRole` (pure function, no persistence)
Implemented as `VisibilityScope.rol_para(site_id, device_group_id)` in
`backend/app/models/visibility_scope.py`. For a request from `user`
targeting resource `R`, resolution runs like this:

```
effective_role(user, site, group_or_none) -> Role | None:
    if user.is_system_admin:
        return SUPER_ADMIN                              # bypasses everything

    site_wide = role_assignments.find(user, site, device_group_id=None)

    # Site-wide lookup: no group specified — return the site-wide grant
    # only (a group-scoped admin does NOT count here; D25 depends on
    # this to gate delegation).
    if group_or_none is None:
        return site_wide.role if site_wide else None

    # Group scope: max-role between the site-wide grant and the
    # group-specific grant. Grants only elevate.
    group_specific = role_assignments.find(user, site, device_group_id=group_or_none)
    candidates = [g.role for g in (site_wide, group_specific) if g is not None]
    return max(candidates, key=role_level) if candidates else None
```

Semantics fall out cleanly:
- Juan with `(site, A, admin)` accessing any resource in A → admin.
- Juan with `(site, B, observer) + (group, B/G2, operator)` accessing device in G2 → operator (elevated); accessing device in G3 → observer (site-wide baseline).
- Juan with `(site, D, admin) + (group, D/G1, observer)` accessing device in G1 → **admin** (the group-scoped observer cannot downgrade the site-wide admin; see D11).
- Juan with only `(group, C/G1, admin) + (group, C/G2, admin)` accessing device in G3 → None (invisible). Site C itself appears in his site list *only as a container* of G1 and G2.

### Role semantics per scope
The same role name means different things at Site scope vs. Group scope, purely because a Group is a narrower container. **No new roles are introduced.** The rule is:

| Role at scope | Can do (within that scope only) | Cannot do (needs escalation) |
|---|---|---|
| **admin at Site** | Everything below + create/delete Groups; grant/revoke any scoped grant within the Site; register/edit/delete Devices in any Group; move Devices between Groups within the Site | Create/delete Sites; move Devices across Sites (unless also site-admin on destination); grant `is_system_admin` |
| **admin at Group** | Register/edit/delete Devices in the Group; move a Device into or out of the Group **only if** the counterparty Group is also one this user is admin on | Create/delete/rename this Group; touch sibling Groups; grant any permission (delegation is site-admin-only per D25 confirmed) |
| **operator at Site or Group** | Change device *configuration*: create/edit/delete VLANs, configure ports, run config-save jobs, etc. Applies to every Device in scope. | Register/delete/move Devices; change grants |
| **observer at Site or Group** | Read-only. List / GET on Sites, Groups, Devices, VLANs, Ports, Jobs in scope. | Any mutation |
| **is_system_admin (User row flag)** | Everything, anywhere. Create/delete Sites; move Devices across Sites; grant `is_system_admin`. | (No restrictions) |

**Design note.** `operator` is deliberately *not* allowed to move Devices. Per the user's rule: operator role is for device *configuration*, not device *placement*. Device placement is admin-only.

### Aggregate / ownership
- `User` is an entity on its own. `RoleAssignment` is owned by `User` (grants are deleted when the user is deleted) and also referenced by `Site` / `DeviceGroup` (grants are deleted when the target scope is deleted — cascade both ways).
- No aggregate root spans across users. Grant management is a service-level concern (`role_assignment_service.py` — new).

## 10.6 Deliberately NOT introduced
- No `Tenant` aggregate above `Site`. The upgrades folder recommended this; the MSP prompt does **not** ask for it. Adding a Tenant layer is a much larger, orthogonal change; if the user wants it, it belongs in a *separate* plan.
- No `DriverRegistry`, `Platform`, `Capabilities`, `NetworkChange`, or `ExecutionBackend` refactor. Those come from the upgrades folder and are orthogonal to the MSP hierarchy invariants. Do not bundle.
- No renaming of existing dataclasses. `Device`, `Job`, etc. keep their names and shapes; only field sets change where §11 dictates.

---

# 11. Target Database Model

The MSP hierarchy is enforced by DB constraints *and* the app; the app remains authoritative for cross-row logic (e.g., "prevent moving between Sites without permission") but the DB backstops the invariants that can be expressed as constraints.

## 11.1 `sites`
- Add `kind VARCHAR NOT NULL DEFAULT 'REGULAR'` — `('REGULAR', 'BASE_INFRASTRUCTURE')`.
- Add `default_group_id INTEGER NULL FK → device_groups.id ON DELETE RESTRICT` — populated inside the Site-creation transaction; enforced NOT NULL post-migration (see §11.6 for the transactional trick).
- Partial unique index `ux_sites_single_base_infra ON sites(kind) WHERE kind = 'BASE_INFRASTRUCTURE'` — guarantees exactly one row of that kind (once seeded).

## 11.2 `device_groups`
- Make `site_id` **NOT NULL** (currently nullable).
- Add `is_default BOOLEAN NOT NULL DEFAULT FALSE`.
- Partial unique index `ux_device_groups_one_default_per_site ON device_groups(site_id) WHERE is_default = TRUE`.
- FK: `site_id INTEGER NOT NULL REFERENCES sites(id) ON DELETE RESTRICT` (change from `SET NULL` to `RESTRICT` — a site with any group cannot be deleted; must delete/reassign groups first).
- Group naming: **unique per site**, not globally (see D6): drop `uq_device_group_name`, add `UniqueConstraint('site_id', 'name')` — *only if D6 = "unique per site"*. If D6 = "globally unique", keep as is.

## 11.3 `devices`
- **Add `device_group_id INTEGER FK → device_groups.id ON DELETE RESTRICT`.**
- **Drop `site_id`** (deferred: keep it during Phase 2 as read-only until the app migration is complete; drop in a separate migration).
- Make `device_group_id NOT NULL` after backfill (see §24).
- Keep `name UNIQUE` for backward compatibility during transition; the *domain* `uniqueness_key` is separate — see §14.
- Optional CHECK: enforce that `devices.device_group_id` references a group whose `site_id` = the user's declared site. **Cannot** be expressed with a single CHECK; it's automatically maintained because Device has only `device_group_id`, and the Group owns the `site_id`.

## 11.4 `device_group_members` (junction)
- **Drop this table** in the final phase. Once Device has `device_group_id`, the junction is redundant and *misleading* (it permits multiple groups per device, which we now forbid).

## 11.5 `role_assignments` (new — required, not optional)

Replaces `user_allowed_sites` entirely. Backs the per-scope role model in §10.5 and §12.

```sql
CREATE TABLE role_assignments (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id               INTEGER NOT NULL
                            REFERENCES users(id) ON DELETE CASCADE,
    site_id               INTEGER NOT NULL
                            REFERENCES sites(id) ON DELETE CASCADE,
    device_group_id       INTEGER NULL
                            REFERENCES device_groups(id) ON DELETE CASCADE,
    role                  VARCHAR NOT NULL
                            CHECK (role IN ('observer', 'operator', 'admin')),
    created_at            DATETIME NOT NULL,
    created_by_user_id    INTEGER NULL
                            REFERENCES users(id) ON DELETE SET NULL,

    -- One grant per (user, exact scope). NULL device_group_id → Site-wide grant.
    -- Postgres treats NULLs as distinct in unique indexes by default; use COALESCE
    -- expression to make (user_id, site_id, NULL) uniquely dedup'd.
    UNIQUE (user_id, site_id, device_group_id)
);

CREATE INDEX ix_role_assignments_user     ON role_assignments (user_id);
CREATE INDEX ix_role_assignments_site     ON role_assignments (site_id);
CREATE INDEX ix_role_assignments_group    ON role_assignments (device_group_id) WHERE device_group_id IS NOT NULL;
```

**Semantics of columns:**
- `site_id NOT NULL` — always the target Site. For a Group-scoped grant, this must equal `device_group.site_id`. A CHECK trigger (or app-level assertion in the service) enforces the consistency; DBs can't express the cross-row check portably.
- `device_group_id NULL` — Site-wide grant covering every current and future Group in the Site.
- `device_group_id` set — Group-specific grant, most-specific wins per §10.5.
- `role` — one of `observer`, `operator`, `admin`. `super-admin` is NOT valid at this scope; it lives on `users.is_system_admin`.
- `created_by_user_id` — audit trail of who issued this grant. NULL only for grants created by data migration.

**Deleted tables** (Phase 5):
- `user_allowed_sites` — obsolete. Migrated into `role_assignments` (see §24).

**Added to `users`** (Phase 1):
- `is_system_admin BOOLEAN NOT NULL DEFAULT FALSE`.

**Deleted from `users`** (Phase 5):
- `role` (string column). Values migrate per §24.

## 11.6 Bootstrapping the "Site has a Default Group" cycle
Two FKs pointing at each other (`sites.default_group_id` → `device_groups.id`, `device_groups.site_id` → `sites.id`). To create either row you need the other. Options, in order of preference:

- **Option A (preferred):** Make `sites.default_group_id` `NULL`-able at the DB level; enforce NOT NULL in the app via the Site-creation transaction (INSERT site, INSERT default group referencing site.id, UPDATE site.default_group_id = group.id — all in one tx). A deferred trigger or app-level assertion can guard against violations. This is the simplest to migrate.
- **Option B:** Use a Postgres DEFERRABLE INITIALLY DEFERRED FK. Standards-compliant but harder to keep working on SQLite (used in dev/tests).
- Option A wins.

## 11.7 (Optional) Row-Level Security
The upgrades folder recommends Postgres RLS as defense-in-depth. This plan does **not** require RLS to satisfy the MSP prompt — the prompt is satisfied by query-predicate filtering with tests. RLS is captured as **D12**: "should MSP enforcement extend to RLS or is app-layer enough?"

---

# 12. Target Authorization Model

The current model — one global `role` per user + a list of `allowed_sites` — cannot express "admin here, observer there," which is a first-class MSP requirement. The target replaces both with per-scope role assignments (§10.5, §11.5). The rest of this section is *how* the check runs, *where* it runs, and *who* is allowed to change grants.

## 12.1 Two ways in: `is_system_admin` or a `role_assignment`

Every request that touches a scoped resource resolves through this decision tree:

```
1. If user.is_system_admin → allowed (any operation, any resource).
2. Else compute effective_role(user, resource) per §10.5.
   - None                → 403 (invisible; treated as "not authorized" not "not found" per D13).
   - observer            → allowed only for read operations.
   - operator            → allowed for read + device-config changes; NOT device moves, NOT grant changes.
   - admin (Site scope)  → all admin ops within the Site (create/delete groups, register/move/delete devices in-Site, grant scoped permissions within the Site).
   - admin (Group scope) → device register/edit/delete/move within the Group only; move-across-groups only when the counterparty group is also admin-scope for this user.
```

There is no more `require_role("admin")` on an entire endpoint. **The required role now depends on the target resource**, not on the endpoint alone.

## 12.2 Coarse gates that survive

Two things are still endpoint-level:
- `require_authenticated` — every non-public endpoint checks that a valid JWT was supplied.
- `require_system_admin` — endpoints that inherently have no scope, e.g. `POST /api/v1/sites`, `DELETE /api/v1/sites/{id}`, `PUT /api/v1/users/{id}/system-admin`, `POST /api/v1/users` when the creator is a system-admin creating a user with no initial grants. These bypass the resource-scope check because there is no resource to check against yet.

Everything else uses **`require_scope(...)`** (new dependency in `core/scope.py`), which:
1. Resolves the target resource from the path params or request body.
2. Computes `effective_role(user, resource)` (or `SUPER_ADMIN` if `is_system_admin`).
3. Compares against the operation's minimum role.
4. 403s on failure.

## 12.3 Operation → required role (authoritative matrix)

| Endpoint / operation | Required |
|---|---|
| `POST /api/v1/sites`, `PUT /api/v1/sites/{id}`, `DELETE /api/v1/sites/{id}` | `is_system_admin` only |
| `GET /api/v1/sites`, `GET /api/v1/sites/{id}` | any grant on the Site (observer or higher), or `is_system_admin` |
| `GET /api/v1/sites/{id}/groups` | observer on Site or on any Group within it, or `is_system_admin` |
| `POST /api/v1/device-groups`, `DELETE /api/v1/device-groups/{id}` | `admin` at **Site** scope on the target Site, or `is_system_admin`. (Group-admin cannot create sibling groups.) |
| `GET /api/v1/device-groups`, `GET /api/v1/device-groups/{id}` | observer on the Group or its Site (max-role resolution — a site-wide grant covers the group), or `is_system_admin` |
| `POST /api/v1/devices` (register) | `admin` at Site scope on the target Site, OR `admin` at Group scope on the specific target Group, or `is_system_admin` |
| `PUT /api/v1/devices/{name}` (edit connection details), `DELETE /api/v1/devices/{name}` | `admin` at the Device's Group scope (or Site scope; the site-wide grant covers all groups under max-role), or `is_system_admin` |
| `POST /api/v1/devices/{name}/move` — **same-Site** move | `admin` on the source Group AND on the target Group (site-admin qualifies since it implies admin on all groups in that Site). Or `is_system_admin`. |
| `POST /api/v1/devices/{name}/move` — **cross-Site** move | `admin` at **Site** scope on both source Site AND destination Site. Group-admin is **not** sufficient. Or `is_system_admin`. |
| `POST /api/v1/vlans/...`, `PUT /api/v1/ports/{...}`, `POST /api/v1/devices/{name}/save` (any device config change) | `operator` or higher on the Device's scope, or `is_system_admin` |
| `GET /api/v1/vlans/{device}`, `GET /api/v1/ports/{device}` | `observer` or higher on the Device's scope, or `is_system_admin` |
| `POST /api/v1/users` (create user) | `is_system_admin`, OR any `admin` at Site scope. Site-admin can *only* create a user pre-configured with a grant within their own Site; they cannot grant on Sites they don't admin. |
| `PUT /api/v1/users/{id}` (edit user profile — email, password, activate/deactivate) | `is_system_admin`, OR `admin` at any Site where this user already has a grant (a site-admin can maintain profiles of users active on their Site). See D26. |
| `DELETE /api/v1/users/{id}` | `is_system_admin` only (avoids the "who owns this user" question; see D26) |
| `PUT /api/v1/users/{id}/system-admin` (grant/revoke `is_system_admin`) | `is_system_admin` only |
| `POST /api/v1/users/{id}/grants` (add a `role_assignment`) | `is_system_admin`, OR `admin` at Site scope on the grant's `site_id`. **Group-admin cannot delegate** (confirmed with the user: auditability > convenience). |
| `DELETE /api/v1/users/{id}/grants/{grant_id}` (revoke) | same rule as add — must be `is_system_admin` or `admin` at the grant's Site |
| `GET /api/v1/users/{id}/grants` | The user themselves (viewing own grants), `is_system_admin`, or `admin` at Site scope on any Site where the user has a grant (returns only the grants scoped to that Site) |
| `GET /api/v1/audit` | see D27 |

**Rule of thumb** (for anything not in the table): the more localized the resource, the smaller the required scope, but grant management is always Site-admin+.

## 12.4 List filtering — SQL predicate, not post-fetch Python

Every list endpoint moves from "fetch all, filter in memory" to a query predicate. Concrete queries:

**Sites visible to a user** (returns the Site container even when only a nested Group grant exists):
```sql
SELECT DISTINCT s.*
  FROM sites s
  JOIN role_assignments ra ON ra.site_id = s.id
 WHERE ra.user_id = :user_id
UNION SELECT * FROM sites WHERE :is_system_admin;
```

**Groups visible in a Site**:
```sql
SELECT g.*
  FROM device_groups g
 WHERE g.site_id = :site_id
   AND (
     :is_system_admin
     OR EXISTS (
       SELECT 1 FROM role_assignments ra
        WHERE ra.user_id = :user_id
          AND ra.site_id = g.site_id
          AND (ra.device_group_id IS NULL OR ra.device_group_id = g.id)
     )
   );
```

**Devices visible to a user** (Global Inventory query):
```sql
SELECT d.*
  FROM devices d
  JOIN device_groups g ON d.device_group_id = g.id
 WHERE :is_system_admin
    OR EXISTS (
      SELECT 1 FROM role_assignments ra
       WHERE ra.user_id = :user_id
         AND ra.site_id = g.site_id
         AND (ra.device_group_id IS NULL OR ra.device_group_id = g.id)
    );
```

Every direct-ID GET (`/devices/{name}`, `/device-groups/{id}`, `/sites/{id}`) runs the same JOIN filter with an equality predicate on the id — no separate "is this allowed?" call.

## 12.5 Base-Infrastructure visibility (D14, updated)

Base Infrastructure is a Site like any other in the target model — visibility requires an explicit grant. **Migration convention (§24 Step 6):** users who could previously see `site_id IS NULL` devices via the current `authz.py:78-83` fallback are auto-granted `observer` on Base Infrastructure so they retain their existing visibility. New users get no automatic Base Infra grant — an admin must add it explicitly.

## 12.6 Direct-ID access

Same rule as list filtering: the direct-ID query runs through the same predicate. Getting `/api/v1/devices/sw01` where `sw01` sits in a Group + Site the caller has no grant on returns **403** (D13). No 404-to-avoid-enumeration trick — IDs and names are internal, no user-enumeration concern.

## 12.7 What the JWT carries

Unchanged today, but simplified: `{sub, iat, exp}`. Role is no longer in the JWT (would be misleading — role varies per resource). `is_system_admin` is *not* in the JWT either — a session with a compromised super-admin token should be revocable by dropping the flag on the user row without waiting for JWT expiry. The `is_system_admin` lookup runs per-request against the DB (one indexed row).

---

# 13. Aggregate & Ownership Recommendations

The MSP prompt's Section 22 explicitly warns against forcing a classical DDD aggregate model. Concrete recommendation:

- **`Site` is an aggregate root** for its `DeviceGroup`s. Deleting or renaming a Site *may* cascade to Groups (see D5); business rules that touch Groups traverse Site.
- **`DeviceGroup` is NOT an aggregate root** for `Device`. `Device` has its own lifecycle (registration, credentials, monitoring) — the "Global Inventory" requirement (`Inventory` service, §5 of the MSP prompt) confirms that Devices exist independently of any one Group.
- Practically this means: `SiteService.delete_site(site)` cascades to its Groups (or refuses, per D5) but **not** to its Devices; Devices must be moved to another Group (in another Site) first, or explicitly de-registered.

Do **not** add `Device` under `DeviceGroup` as an owned entity. The domain layer accesses `Device` via `Inventory`, not via `DeviceGroup`.

---

# 14. Device Identity & Uniqueness Strategy

Today: uniqueness is on `devices.name` (a human-readable string) and `devices.id` (surrogate). No `mac`, `serial_number`, `hardware_uuid`, or `management_ip` uniqueness. Vendor is validated but not part of any key.

The MSP prompt §7 says: **do not arbitrarily choose an identity key**. Instead, analyze available attributes.

**Attributes currently available on `Device`:**

| Attribute | Type | Stable? | Globally unique? | Notes |
|---|---|---|---|---|
| `name` | str | Chosen by operator | Yes (globally unique today) | Human-readable label — chosen at registration. Fine as UX identifier; unsafe as domain identity because it can be typo'd or renamed. |
| `host` | str | Changes with re-IP | No | IP/DNS; can change; multiple devices could share hostname across sites in a target MSP with tenant isolation. |
| `id` (surrogate int) | int | Internal | Yes | Autoincrement PK. Perfect as internal identity; useless for de-dup checks. |
| MAC address | — | Would be stable | Would be unique | **Not currently stored.** Devices with multiple MACs (management + data plane) complicate this. |
| Serial number | — | Stable (physical) | Yes across the fleet | **Not currently stored.** Vendor-format specific but universally supported. |

**Options:**

1. **Status quo — keep `name` as human unique key** (recommended for now). Cheapest. Consistent with "Global Inventory shows devices by name". Ships with MSP without a new field. Weakness: no protection against re-registering the same physical box under a different name.
2. **Add `serial_number` (nullable, unique when not null)** — allows opportunistic dedup on serial. Backfilling requires an inventory scan (out of scope for MSP structure alone). Recommended as a **follow-up**, not blocking MSP.
3. **Combined natural key `(vendor, serial_number)`** — most rigorous. Requires all devices to have a serial_number at registration — a hard requirement change.

**Recommended decision (subject to D15):** ship MSP with option 1 (`name` remains the human-unique identifier). Add option 2 as a Phase-6 optimization once real duplicate-registration incidents (or a compliance ask) justify the discovery/scan work. Document explicitly that "renaming a device does not preserve identity across MSP tenants" (though this platform is single-tenant).

**IP address is NOT recommended as identity** because devices can, and do, change IPs (management VLAN moves, DHCP, staging→prod cutovers).

---

# 15. Site Creation Flow (Target)

Atomic operation, application-service-owned (`site_service.create_site`), corresponding domain method `Site.create(...)`:

```
Application service: site_service.create_site(name, description, kind=REGULAR)
  1. BEGIN TX
  2. INSERT sites (name, description, kind, default_group_id=NULL)  → returns site.id
  3. INSERT device_groups (name='Default', description=<system>, site_id=site.id, is_default=TRUE)  → returns group.id
  4. UPDATE sites SET default_group_id = group.id WHERE id = site.id
  5. COMMIT
  6. audit_service.log_action("create_site")
  7. audit_service.log_action("create_device_group", parent_audit_id=<previous>) — for the Default Group
```

Steps 2–4 run in a single Postgres transaction. Failure of any step rolls back the whole thing — a Site never becomes visible without its Default Group. On SQLite, the same rollback semantics hold within `get_session()` (`db/session.py` uses SQLAlchemy sessions with commit-on-close).

For **Base-Infrastructure Site**, the same flow runs at first-boot bootstrap (`main.py:seed_defaults` equivalent — see §24) with `kind=BASE_INFRASTRUCTURE`, and the partial unique index prevents duplicates.

---

# 16. Device Registration Flow (Target)

The MSP prompt §13 requires a **Site-first** flow.

**API surface** (POST /api/v1/devices):

```json
{
  "name": "sw01",
  "host": "10.0.0.5",
  "vendor": "cisco_ios",
  "platform": "ios",
  "username": "admin",
  "password": "***",
  "site_id": 42,               // required
  "device_group_id": null       // optional; null → Site's default_group
}
```

**Server flow:**

```
POST /api/v1/devices
  1. require_role("admin")
  2. Validate site_id exists  → 400 if not
  3. Resolve device_group_id:
       if device_group_id is None:
           device_group_id = site.default_group_id
       else:
           assert device_group.site_id == site_id  → 400 "Group does not belong to this Site"
  4. authz.require_scope(user, site_id, device_group_id)  → 403 if not allowed
  5. inventory.register(name, host, vendor, ..., device_group_id)  → INSERT devices
  6. audit_service.log_action("register_device")
```

**Frontend flow** (mirrors the backend contract):

```
Device registration modal
  Step 1: Select Site (dropdown scoped to user's allowed Sites)
  Step 2: On Site select → GET /api/v1/sites/{id}/groups  → dropdown of Groups in that Site
          Default Group is pre-selected
  Step 3: Fill name/host/vendor/credentials
  Step 4: Submit
```

**Backend never trusts frontend-supplied group_id in isolation** — it always re-validates step 3 against the DB.

---

# 17. Device Movement / Reassignment Flow (Target)

Old API (today): `PUT /devices/{name}` with a body containing `site_id` — implicitly moves the device.

**Target API:** single, explicit call.

```
POST /api/v1/devices/{name}/move
  { "device_group_id": <target> }
```

Server flow:

```
1. require_role("operator")   ← or "admin", D16
2. device = inventory.get(name)  → 404 if missing
3. target_group = device_group_service.get(device_group_id)  → 400 if missing
4. authz.require_scope(user, source_group.site_id, source_group.id)   ← must own current location
5. authz.require_scope(user, target_group.site_id, target_group.id)   ← and target
6. inventory.move(device, target_group)  → UPDATE devices SET device_group_id = target_group.id
7. audit_service.log_action("move_device", details={from_group, to_group, from_site, to_site})
```

**Impacts to check when moving across Sites** (per MSP prompt §14):
- **Caches**: today the only mutable caches are `device_locks` (Redis, keyed by device name — no site awareness, no invalidation needed) and rate-limit slots (same). No stale cache.
- **In-flight Jobs**: pending/running Jobs reference `device` by name (`JobModel.device`) and `parameters` (JSON). Site-scoped queries that filter jobs by allowed sites would need to re-evaluate at query time. **Recommendation:** disallow moving a Device while it has a non-terminal Job (409) — the simplest correctness rule.
- **Audit records** — no change needed; `AuditLogModel` records the action itself, not a live join to Site.
- **Group jobs / device_results** — a device that was in group G at enqueue-time may move mid-run; because `device_results` are stored on the `GroupJobModel` by device name, no rewrite is required.
- **Frontend**: whichever list the device was in stops showing it; global inventory continues to show it. No stale UI risk.

## Allowed movements (D17)
- Same Site, different Group: always allowed if user has scope.
- Different Site: **requires `admin` role** in the recommended default; ordinary operators cannot pull a device out of their Site into another. Confirmable via D17.

---

# 18. Global Inventory Design

The MSP prompt §5 says the **Global Inventory is the authoritative Device management surface**. Currently `GET /api/v1/devices` already fills that role — it lists every visible device. The changes are:

- Column set includes: `name`, `site` (derived via join), `device_group` (name), `vendor`, `platform`, `host`, `last_seen` (if telemetry ever lands — out of scope here).
- Filters: `site_id?`, `device_group_id?`, `vendor?`, `search=<name substring>`.
- Every result is scoped to the user's `role_assignments` via the query in §12.4.
- Actions from Inventory: register (POST /devices), move (POST /devices/{name}/move), edit (PUT /devices/{name}), delete (DELETE /devices/{name}) — same endpoints as today, unified UX.
- Frontend: today `/devices` is a flat table (`frontend/src/app/(dashboard)/devices/page.tsx`). Keep it; add a "Move…" action opening a Group picker scoped to the user's allowed Sites.

**Site views** (`/sites/{id}` — new route, currently not present) become **contextual**: show the Site's Groups; each Group is expandable to its Devices. This satisfies the MSP prompt §18 UX. The existing `/device-groups` route (`frontend/src/app/(dashboard)/device-groups/page.tsx`) can be repurposed as a "manage groups within a site" surface — or, if D18 chooses, deleted entirely and folded into the new Site detail page.

---

# 19. API Changes

Full list of endpoint deltas. **Nothing removes an endpoint without a compatible replacement in the same phase** unless flagged.

| Endpoint | Current | Target | Backward compat? |
|---|---|---|---|
| `POST /api/v1/sites` | Creates Site | Creates Site **+ Default Group** atomically | Same request/response; new side effect. Non-breaking. |
| `GET /api/v1/sites` | Returns all Sites unfiltered | Returns Sites in user's scope | Response *shrinks* for restricted users. Breaking for API clients that assume "list all". |
| `GET /api/v1/sites/{id}` | Any observer can read | Restricted users get 403/404 if outside scope | Breaking for automation as above. |
| `DELETE /api/v1/sites/{id}` | 409 if devices exist | 409 if devices OR groups exist; 400 if Site is Base Infra | Breaking. |
| `GET /api/v1/sites/{id}/groups` | *(missing)* | New: list Groups in a Site (scoped) | New endpoint. |
| `POST /api/v1/device-groups` | `site_id` required in body | `site_id` still required; Site-first UX is client-side | Same schema. |
| `GET /api/v1/device-groups` | Post-fetch filter | SQL-scoped list | Same shape; different perf profile. |
| `DELETE /api/v1/device-groups/{id}` | Deletes the group; devices become orphaned members (but ORM already cascades member rows) | **Reject** if group is the Default Group; **reject** if it holds any Device (must be moved out first, or force-move to Default) — D19 | Breaking. |
| `POST /api/v1/device-groups/{id}/members` | Adds Device→Group membership row (M2M) | **Deprecate.** Replaced by `POST /devices/{name}/move`. Kept as compatibility alias for one release. | Deprecation. |
| `DELETE /api/v1/device-groups/{id}/members/{device_name}` | Removes membership row | **Deprecate.** Removing from a group means moving to Default. | Deprecation. |
| `GET /api/v1/device-groups/{id}/devices` | Lists devices in a group | Same, but backed by `Device.device_group_id`, not M2M | Same schema. |
| `POST /api/v1/devices` | `site_id` optional; no group required | `site_id` required; `device_group_id` optional (defaults to Site's Default Group) | Breaking: `site_id` becomes required. |
| `GET /api/v1/devices` | Post-fetch filter | SQL-scoped list; add optional `site_id?`, `device_group_id?` query params | Compatible additions. |
| `GET /api/v1/devices/{name}` | Post-fetch check | Scoped lookup; response gains `device_group_id`, `device_group_name` | Additive. |
| `PUT /api/v1/devices/{name}` | Body may set `site_id` (implicit move) | Body **cannot** set `site_id` (derived); may set `device_group_id`? — see D20 | Breaking. |
| `POST /api/v1/devices/{name}/move` | *(missing)* | Explicit move endpoint | New endpoint. |
| `DELETE /api/v1/devices/{name}` | Deletes device | Unchanged, except must check user has scope | Same. |
| `PUT /api/v1/users/{id}/allowed-sites` | Sets `user_allowed_sites` | **Deprecate and remove** — grants now managed via `POST/DELETE /api/v1/users/{id}/grants` (§12.3 matrix). Kept as compatibility shim for one release: PUT with `[siteA, siteB]` becomes N observer-role Site grants (preserving current semantics for existing clients). | Breaking after deprecation. |
| `POST /api/v1/users/{id}/grants` | *(missing)* | New: create a `role_assignment` with body `{site_id, device_group_id?: null, role: 'observer'\|'operator'\|'admin'}`. Auth: `is_system_admin` OR `admin` at Site scope on `site_id`. | New endpoint. |
| `DELETE /api/v1/users/{id}/grants/{grant_id}` | *(missing)* | New: revoke a grant. Same auth rule. | New endpoint. |
| `GET /api/v1/users/{id}/grants` | *(missing)* | New: list grants. Auth: the user themselves, `is_system_admin`, or Site-admin (returns only grants for their Sites). | New endpoint. |
| `PUT /api/v1/users/{id}/system-admin` | *(missing)* | New: `{is_system_admin: bool}`. Auth: `is_system_admin` only. Cannot demote the last active system-admin (400). | New endpoint. |
| `POST /api/v1/users` | Creates user with role + optional allowed_site_ids | New body: `{username, email?, password, initial_grants?: [{site_id, device_group_id?, role}]}`. `is_system_admin` NEVER set here — separate endpoint. Auth per §12.3 matrix (site-admin may only set initial_grants for their own Site). | Breaking. |
| `PUT /api/v1/users/{id}` | Full user edit | Restrict body to profile fields (email, password, is_active); role and site-scoping fields rejected — use grants endpoints. | Breaking. |
| `DELETE /api/v1/users/{id}` | Deletes user | Restrict to `is_system_admin`. Cascade deletes `role_assignments` rows (FK ondelete). | Breaking (rare use of this by non-super-admins anyway). |

**Every changed endpoint requires updated OpenAPI examples**, test coverage additions, and frontend query updates. See §26 for the test list and §32 for the tasks.

---

# 20. Backend / Application Changes

Per module — grouped by phase (see §31). Every file listed below needs work; paths are exact.

## 20.1 New files
- `backend/app/services/inventory_service.py` — the `Inventory` application service (`register`, `deregister`, `get`, `list(user=...)`, `move`). Thin wrapper today over `device_service` + `device_group_service`; over time the free functions move into methods here.
- `backend/app/services/role_assignment_service.py` — CRUD for `role_assignments` rows (`grant`, `revoke`, `list_for_user`, `list_for_site`). Enforces "site-admin can only grant within their Site" and "cannot grant `is_system_admin` here".
- `backend/app/services/effective_role.py` — pure function `effective_role(user, resource_type, resource_id) -> Role | None` per §10.5. No DB writes; wraps the JOIN queries in §12.4.
- `backend/app/core/scope.py` — FastAPI dependencies: `require_authenticated`, `require_system_admin`, `require_scope(operation_name)`. `require_scope` reads the target resource from the request, computes effective_role, compares against the operation's minimum-role table (colocated with the dependency).
- `backend/app/schemas/inventory.py` (or extend `schemas/device.py`) — new request bodies for move, and enriched `DevicePublic` fields (`device_group_id`, `device_group_name`).
- `backend/app/schemas/role_assignment.py` — `RoleAssignmentCreate`, `RoleAssignmentRead`, `SystemAdminUpdate`.

## 20.2 Files to modify

`backend/app/db/models.py`
- Add `SiteModel.kind`, `SiteModel.default_group_id` (nullable FK).
- Add `DeviceGroupModel.is_default` (default False).
- Make `DeviceGroupModel.site_id` NOT NULL.
- Add `DeviceModel.device_group_id` (FK RESTRICT, nullable → NOT NULL after backfill).
- Remove `DeviceModel.site_id` (Phase 4).
- Remove `DeviceGroupMemberModel` (Phase 4).

`backend/app/services/site_service.py`
- `create_site` → atomic Site+DefaultGroup transaction (per §15). Populate `default_group_id`.
- `delete_site` → reject if `kind == BASE_INFRASTRUCTURE`; reject if any Group still has Devices; cascade groups otherwise (D5).
- New: `get_or_create_base_infrastructure()` for the seed.

`backend/app/services/device_group_service.py`
- `create_group` — no logic change (site_id already required); enforce `is_default=False` on user-created groups.
- `delete_group` — reject if `is_default`; reject if any Device references it; else delete.
- Delete `add_member` and `remove_member` after Phase 4 (M2M table gone).
- New: `move_device_to_group(device_name, target_group_id, actor)` (or place this on `inventory_service`).
- `list_group_devices` — read via `devices.device_group_id`, not junction.

`backend/app/services/device_service.py`
- `create_device(..., device_group_id)` — replaces `site_id` param (backend derives site from group).
- `update_device` — drop `site_id` param; drop the M2M invariant check (§7.4 becomes moot when there's exactly one group per device).
- `_to_domain` — populate `site_id`/`site_name` from `device_group.site` (derived).
- `move_device(name, target_group_id)` — either here or in `inventory_service`.

`backend/app/core/authz.py` — **substantially rewritten**
- Delete `_UNRESTRICTED_ROLES`, `is_unrestricted`, `allowed_site_ids_for`, `allowed_device_names_for`, `ensure_device_allowed`, `ensure_devices_allowed`, `set_user_allowed_sites`, `get_user_allowed_sites`.
- Replace with helpers backed by `role_assignments`:
  - `visible_site_ids_for(user_id) -> set[int]` — one JOIN, returns Sites where user has any grant (or all Sites for `is_system_admin`).
  - `visible_device_names_for(user_id) -> set[str]` — the query from §12.4 above; primarily for tests and legacy call-sites during Phase 3 transition.
  - `effective_role(user, resource_type, resource_id) -> Role | None` — the core function, callable from `require_scope`.
  - `is_system_admin(user) -> bool` — direct DB check on the users row.
- The `site_id IS NULL` fallback goes away; after migration no device is site-less. (Phase 5.)

`backend/app/api/*` — see §19 for endpoint deltas.

`backend/app/main.py`
- On startup (`seed_defaults` equivalent), ensure Base-Infrastructure Site + its Default Group exist. Idempotent.
- On startup, ensure every existing Site without a Default Group gets one (retroactive; belt-and-braces after migration).

`backend/app/schemas/device.py`
- `DeviceCreate`: `site_id: int` (required, not optional); add `device_group_id: Optional[int] = None`.
- `DeviceUpdate`: remove `site_id`.
- `DevicePublic`: add `device_group_id`, `device_group_name`; keep `site_id`, `site_name` (derived on read).

`backend/app/schemas/device_group.py`
- `DeviceGroupRead`: add `is_default: bool`.

`backend/app/schemas/site.py`
- `SiteRead`: add `kind: str`, `default_group_id: int`.

## 20.3 Files that are unaffected
- `backend/app/services/vlan_service.py`, `port_service.py`, `vlan_execution_service.py`, `port_execution_service.py`, `orchestration_runner.py`, `device_locks.py`, `rate_limiter.py`, `retry_policy.py` — these operate on Device *names* and know nothing of Site/Group. **No change required.**
- `backend/app/services/vendors/` — driver code stays put.
- `backend/ansible/**` — no playbook changes.
- `backend/app/worker.py`, Celery task setup — no change.
- `backend/app/services/audit_service.py`, `db/audit_guard.py` — no change.

**This is the key: G13 (no regression on the vendor/Ansible stack) is easy to hold because the MSP domain reshape is *above* the vendor layer.**

---

# 21. Frontend Changes

Framework: Next.js 16 App Router; React Query 5; axios.

## 21.1 Existing pages to modify
- `frontend/src/app/(dashboard)/devices/page.tsx` — device create/edit form: replace "Site (optional)" dropdown with **required** Site dropdown; add **Group dropdown that reloads on Site change** and defaults to the Site's Default Group. Add a "Move…" action per row that opens a Group picker (scoped to the user's Sites). Remove the ability to clear `site_id` (no longer valid).
- `frontend/src/app/(dashboard)/device-groups/page.tsx` — the "add member" flow (`groupDevicePool = deviceList.filter((d) => d.site_id === group.site_id)` at :334) becomes redundant post-Phase-4. Either delete this page entirely (folding into Site detail) — see D18 — or repurpose it as a per-Site group management surface.
- `frontend/src/app/(dashboard)/sites/page.tsx` — indicate the Default Group in the Sites list (badge). Sites of `kind=BASE_INFRASTRUCTURE` show a badge and no delete button. On restricted users, list only the Sites in scope.
- `frontend/src/app/(dashboard)/users/page.tsx` — **substantial rewrite**. Replace the current "allowed_site_ids" checkbox list with a grants editor: per row, `(site, group?, role)`. Add controls to grant/revoke at Site scope and at Group scope. Add a separate "System Admin" toggle (visible only to system-admins) that hits `PUT /users/{id}/system-admin`. Roles pickable: observer, operator, admin.
- `frontend/src/app/(dashboard)/page.tsx` — the "unassignedCount" widget (line 146 currently filters `d.site_id == null`) becomes 0 post-migration; either remove the widget or repurpose to "Base-Infrastructure device count".
- `frontend/src/services/api.ts` — update `Device`, `DeviceCreate`, `DeviceUpdate`, `DeviceGroup`, `Site` type definitions. Add `moveDevice(name, groupId)`; drop `addGroupMember`, `removeGroupMember` in Phase 4.
- `frontend/src/app/(dashboard)/vlans/page.tsx`, `ports/page.tsx` — site filters (`siteFilter` at vlans:69, ports:117) keep working; devices are now filtered via `device.device_group.site_id` — the frontend types shift but the filter UX stays.

## 21.2 New pages
- `frontend/src/app/(dashboard)/sites/[id]/page.tsx` — Site detail with its Groups and their Devices. Shows a "Move Device" action button per Device. Nested `<Groups>` component.
- (Optional) `frontend/src/app/(dashboard)/inventory/page.tsx` — if D18 chooses to differentiate "Global Inventory" from "Devices under a Site". Otherwise `/devices` already fills that role.

## 21.3 Non-changes
- Ports, VLANs, Jobs, GroupJobs, Audit UI — no structural change.

---

# 22. Authorization Changes

Summary — collected from §10.5, §11.5, §12, §19, §20 for reviewer convenience:

1. Replace `user_allowed_sites` and `UserModel.role` with `role_assignments` table + `is_system_admin` bool.
2. Rewrite `core/authz.py` around `effective_role(user, resource)` (per §10.5). Delete `is_unrestricted`, `allowed_device_names_for`, `set_user_allowed_sites`, etc.
3. Move device/site/group **list filtering from Python to SQL** (query predicate — SQL in §12.4). Files: `api/devices.py`, `api/device_groups.py`, `api/sites.py`.
4. Fix **`GET /api/v1/sites` scope leak** (G10) — now a natural consequence of the JOIN filter.
5. Introduce `require_scope(operation_name)` FastAPI dependency and use it in place of ad-hoc `ensure_device_allowed(...)` and `require_role("...")` calls. Endpoints that are inherently scopeless (create Site, grant system-admin) keep a `require_system_admin` gate.
6. Add grant-management endpoints (§19: `POST/DELETE/GET /users/{id}/grants`, `PUT /users/{id}/system-admin`).
7. Provide a compatibility shim for `PUT /users/{id}/allowed-sites` for one release (translates to N observer-role Site grants).
8. Delete the `site_id IS NULL` fallback in Phase 5 (once no code path relies on it).
9. (Optional, D12) add Postgres RLS as a defense-in-depth safety net.

---

# 23. Database Migration Strategy

Phased schema evolution. Each migration is a single Alembic revision. Order matters.

**M1 — add columns (nullable), no drops, no NOT NULL:**
- `ALTER TABLE sites ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'REGULAR';`
- `ALTER TABLE sites ADD COLUMN default_group_id INT NULL FK → device_groups.id ON DELETE RESTRICT;`
- `ALTER TABLE device_groups ADD COLUMN is_default BOOL NOT NULL DEFAULT FALSE;`
- `ALTER TABLE devices ADD COLUMN device_group_id INT NULL FK → device_groups.id ON DELETE RESTRICT;`

**M2 — data migration (§24 details):**
- Ensure Base-Infrastructure Site exists.
- For every Site without a `default_group_id`, create a "Default" Group and point `default_group_id` at it.
- For every Device with `site_id IS NULL`, assign it to Base-Infrastructure's Default Group.
- For every Device with `site_id IS NOT NULL`:
  - If it's a member of exactly one Group in that Site → assign that Group.
  - Else → assign the Site's Default Group. Log a warning per device where the previous M2M state was ambiguous.
- Idempotent — safe to rerun.

**M3 — enforce invariants:**
- `ALTER TABLE devices ALTER COLUMN device_group_id SET NOT NULL;`
- `ALTER TABLE device_groups ALTER COLUMN site_id SET NOT NULL;` (may fail if legacy null-site groups still exist; M2 must first delete or merge them per D21).
- Add partial unique indexes:
  - `CREATE UNIQUE INDEX ux_sites_single_base_infra ON sites(kind) WHERE kind = 'BASE_INFRASTRUCTURE';`
  - `CREATE UNIQUE INDEX ux_device_groups_one_default_per_site ON device_groups(site_id) WHERE is_default = TRUE;`
- Change `device_groups.site_id` FK on-delete: `SET NULL` → `RESTRICT`.

**M4 — drop legacy columns/tables:**
- `ALTER TABLE devices DROP COLUMN site_id;`
- `DROP TABLE device_group_members;`

**M4 runs only after** all code paths have stopped referencing `devices.site_id` or `device_group_members` (Phase 4 in §31). Delaying M4 to a later release also allows a rollback window.

**Reversibility.** M1 and M2 are reversible (drop columns / restore null site_id from group's site). M3's `NOT NULL` and unique indexes are reversible. M4 is **not** cleanly reversible without a re-backfill; treat it as a point of no return. Keep at least two full backups spanning M4.

---

# 24. Data Migration Strategy

Concrete SQL/pseudo-SQL applied inside M2. All statements go in the Alembic revision that runs after M1.

**Step 1 — Base-Infrastructure Site + its Default Group.**
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

UPDATE sites s
   SET default_group_id = (SELECT id FROM device_groups g
                            WHERE g.site_id = s.id AND g.is_default = TRUE)
 WHERE s.default_group_id IS NULL
   AND s.kind = 'BASE_INFRASTRUCTURE';
```

**Step 2 — every regular Site without a Default Group gets one.** Same INSERT/UPDATE pair, filtered by `s.kind = 'REGULAR'`.

**Step 3 — every Device assigned to a Group.**
```sql
-- 3a: devices with no Site → Base Infrastructure's default group
UPDATE devices SET device_group_id = (
    SELECT default_group_id FROM sites WHERE kind = 'BASE_INFRASTRUCTURE'
) WHERE site_id IS NULL;

-- 3b: devices with a Site and exactly one Group membership within that Site → that Group
WITH single_group_devices AS (
    SELECT m.device_name, MIN(m.group_id) AS the_group
      FROM device_group_members m
      JOIN device_groups g ON g.id = m.group_id
      JOIN devices d ON d.name = m.device_name
     WHERE d.site_id = g.site_id
     GROUP BY m.device_name
    HAVING COUNT(DISTINCT m.group_id) = 1
)
UPDATE devices d
   SET device_group_id = s.the_group
  FROM single_group_devices s
 WHERE d.name = s.device_name;

-- 3c: devices with a Site but ambiguous (multiple groups) or zero groups → Site's default group
UPDATE devices d
   SET device_group_id = s.default_group_id
  FROM sites s
 WHERE d.site_id = s.id
   AND d.device_group_id IS NULL;
```

**Step 4 — audit log every ambiguous assignment.** For each device assigned via 3c, INSERT an `audit_logs` row with `action='msp_migration_ambiguous_group_assignment'`, `resource='device'`, `resource_id=<device.name>`, `details={'previous_groups': [...], 'assigned_to': <default_group_id>}`.

**Step 5 — handle legacy null-site groups (D21).** Options:
- **D21a:** Move memberships out (per Step 3), then delete the null-site groups entirely.
- **D21b:** Assign them to a "Migration Sink" Site (auto-created) so their history is preserved.

Recommendation: D21a. Ambiguous groups were already invisible to non-admins; deleting them post-migration is minimally surprising and simplest.

**Step 6 — user permissions and system-admin migration.**

The old model has one global `users.role` column and a `user_allowed_sites` list. The new model has `is_system_admin` on the user row and per-scope `role_assignments`.

Migration policy (see D24):
```sql
-- 6a: promote today's admin/super-admin users to system-admin.
--     Rationale: authz.is_unrestricted() currently treats both as unrestricted.
--     Downgrading them would silently strip privileges. Migrating conservatively
--     preserves current behavior; a system-admin can demote afterwards.
UPDATE users SET is_system_admin = TRUE
 WHERE role IN ('admin', 'super-admin');

-- 6b: operator/observer users — convert their user_allowed_sites into
--     Site-scoped role_assignments carrying their existing role name.
INSERT INTO role_assignments (user_id, site_id, device_group_id, role, created_at, created_by_user_id)
     SELECT u.id, uas.site_id, NULL, u.role, now(), NULL
       FROM users u
       JOIN user_allowed_sites uas ON uas.user_id = u.id
      WHERE u.role IN ('operator', 'observer')
        AND NOT EXISTS (
          SELECT 1 FROM role_assignments ra
           WHERE ra.user_id = u.id AND ra.site_id = uas.site_id AND ra.device_group_id IS NULL
        );

-- 6c: operator/observer users with an EMPTY user_allowed_sites who previously
--     saw site_id IS NULL devices via authz.py's fallback branch → grant them
--     observer on Base Infrastructure so they retain visibility. D14.
INSERT INTO role_assignments (user_id, site_id, device_group_id, role, created_at, created_by_user_id)
     SELECT u.id,
            (SELECT id FROM sites WHERE kind = 'BASE_INFRASTRUCTURE'),
            NULL, 'observer', now(), NULL
       FROM users u
      WHERE u.role IN ('operator', 'observer')
        AND NOT EXISTS (SELECT 1 FROM user_allowed_sites WHERE user_id = u.id);

-- 6d: verification — no operator/observer user should be left with zero grants
--     unless they had zero sites AND we chose not to auto-grant Base Infra.
```

The `user_allowed_sites` table is **kept in place** through Phase 3 (read by legacy code paths not yet migrated) and **dropped in Phase 5**. `users.role` is likewise kept until Phase 5 to allow rollback; the new authz layer reads `is_system_admin` and `role_assignments` only.

**Verification queries** run at the end of M2 and fail the migration if any return > 0:
- `SELECT COUNT(*) FROM devices WHERE device_group_id IS NULL;`
- `SELECT COUNT(*) FROM device_groups WHERE site_id IS NULL;`
- `SELECT COUNT(*) FROM sites WHERE default_group_id IS NULL;`
- `SELECT COUNT(*) FROM sites WHERE kind = 'BASE_INFRASTRUCTURE'` **must equal 1**.
- `SELECT COUNT(*) FROM users WHERE role IN ('admin', 'super-admin') AND is_system_admin = FALSE;` (should be 0 after 6a)
- `SELECT COUNT(*) FROM users u WHERE u.role IN ('operator', 'observer') AND is_system_admin = FALSE AND NOT EXISTS (SELECT 1 FROM role_assignments ra WHERE ra.user_id = u.id);` (should be 0 unless D14 explicitly permits zero-grant users)

---

# 25. Backward Compatibility

**Breaking changes to API clients** (see §19 for full table):
- `POST /api/v1/devices` — `site_id` becomes required.
- `PUT /api/v1/devices/{name}` — `site_id` in body is rejected.
- `GET /api/v1/sites` — response is filtered for restricted users.
- `POST /api/v1/device-groups/{id}/members` — deprecated, then removed in a later release.

**Recommendation:** issue a **minor version bump** (v1 → v1.1 in OpenAPI info; `/api/v1/` prefix preserved). Ship one-release deprecation warnings on removed endpoints. Frontend and any first-party clients update in the same release; document the change in `CHANGELOG.md` (currently absent — see D22).

**Non-breaking** (additive) changes: new `POST /devices/{name}/move`, new `GET /sites/{id}/groups`, new `SiteRead.kind` field.

**External automation** (if any exists — verify with the user): PUT-based site changes and add-member flows break. Provide a migration script or a compatibility shim (POST /device-groups/{id}/members → internally translated to `move`) for the deprecation window.

---

# 26. Testing Strategy

## 26.1 Domain tests (unit)
- `test_site_creation_creates_default_group.py`
- `test_default_group_cannot_be_deleted.py`
- `test_group_belongs_to_exactly_one_site.py`
- `test_device_group_site_id_is_immutable.py`
- `test_device_belongs_to_exactly_one_group.py`
- `test_device_cannot_be_orphaned_from_group.py`
- `test_move_device_across_sites.py`
- `test_move_device_within_same_site.py`
- `test_move_device_rejected_when_active_job.py` (if D_active_job = enforced)
- `test_base_infrastructure_site_is_unique_and_undeletable.py`
- `test_device_name_still_globally_unique.py`

## 26.2 Authorization tests

**System-admin bypass:**
- `test_system_admin_sees_everything.py`
- `test_system_admin_can_create_delete_sites.py`
- `test_non_system_admin_cannot_grant_system_admin.py`
- `test_cannot_demote_last_active_system_admin.py`

**Per-scope role — visibility (list queries):**
- `test_sites_list_returns_only_sites_with_any_grant.py`
- `test_sites_list_includes_site_when_only_group_grant_exists.py` — Site C in the Juan example
- `test_groups_list_scoped_to_grants.py` — Juan sees only G1, G2 in Site C
- `test_devices_list_scoped_to_grants.py`

**Per-scope role — most-specific wins:**
- `test_effective_role_group_grant_overrides_site_grant.py` — Juan is operator on B/G2 despite observer on B
- `test_effective_role_site_grant_applies_when_no_group_grant.py` — Juan observer on B/G3 via the Site grant
- `test_effective_role_none_when_no_grant.py` — Juan invisible on C/G3, C/G4

**Cross-role scenarios (the Juan / Alice / Bob cases from the design conversation):**
- `test_user_can_hold_admin_on_one_site_observer_on_another.py`
- `test_group_grant_upgrade_from_site_observer_to_group_operator.py`
- `test_site_visible_as_container_when_only_group_grants_exist.py`

**Operation matrix (§12.3):**
- `test_operator_cannot_move_device.py` — operator role never authorizes a move
- `test_admin_cross_site_move_requires_site_admin_on_both.py`
- `test_admin_cross_site_move_group_admin_insufficient.py`
- `test_admin_same_site_move_requires_admin_on_both_groups.py`
- `test_site_admin_can_create_group.py`
- `test_group_admin_cannot_create_sibling_group.py`
- `test_group_admin_cannot_grant_permissions.py` — delegation is site-admin-only

**Grant management:**
- `test_site_admin_can_grant_within_own_site.py`
- `test_site_admin_cannot_grant_outside_own_site.py`
- `test_site_admin_cannot_grant_system_admin.py`
- `test_user_can_view_own_grants.py`
- `test_revoke_grant_removes_visibility.py`

**Direct-ID enumeration guards (§25):**
- `test_direct_device_get_denies_outside_scope.py` — 403
- `test_direct_group_get_denies_outside_scope.py`
- `test_direct_site_get_denies_outside_scope.py`

**Compatibility shim (Phase 3 only):**
- `test_legacy_put_allowed_sites_creates_observer_grants.py`

## 26.3 Integration tests
- `test_msp_end_to_end_registration.py` — create Site → default group appears → register Device without group_id → lands in Default Group → shows in inventory scoped to allowed user.
- `test_msp_end_to_end_move.py` — create two Sites → move Device between them → allowed user sees the change → other user loses/gains visibility.
- `test_msp_end_to_end_delete_site.py` — cannot delete Site with devices; can delete after moving all out.
- `test_msp_end_to_end_delete_base_infra_forbidden.py`.

## 26.4 Migration tests
Under `backend/tests/migrations/`:
- `test_migration_backfill_creates_base_infra.py` — from a snapshot of pre-M2 schema + seed data, run M1+M2, assert Base Infra exists.
- `test_migration_backfill_assigns_devices_to_default_group.py`.
- `test_migration_backfill_leaves_no_null_group_devices.py`.
- `test_migration_backfill_handles_ambiguous_multi_group_devices.py` — the audit-log side effect.

## 26.5 Non-regression tests
- Existing tests under `backend/tests/` (60+ files) must all still pass. Especially:
  - `test_authz_*.py` — semantics change but assertions must be updated in the same PR.
  - `test_device_service*.py`.
  - `test_device_group_service*.py`.
  - `test_sites*.py`.
  - `test_vlan_*.py`, `test_port_*.py` — must remain untouched (no vendor-layer changes).
  - `test_celery_dispatch.py`, `test_rollback_hardened.py` — must remain untouched.

## 26.6 Test discipline
- Test with a real DB (Postgres in CI, SQLite locally). Do not mock the ORM.
- Assert scoping at the SQL layer with `sqlalchemy.event`-based query capture where practical (verify the JOIN clauses are present).

---

# 27. Security Considerations

Per MSP prompt §25.

- **No ID enumeration bypass.** After Phase 3, direct `GET /api/v1/sites/{id}`, `/device-groups/{id}`, `/devices/{name}` all resolve through `effective_role` and 403 for out-of-scope IDs regardless of what number the client picks.
- **Indirect access via a related resource** (e.g., getting a Group by ID that belongs to a forbidden Site) — the target implementation JOINs to `role_assignments`, so an out-of-scope Group is not visible.
- **Base-Infrastructure visibility.** Explicit grant only (D14) — even system-admin migration only grants Base Infra observer to users who previously fell through the `site_id IS NULL` hatch. A newly-created non-system-admin user has zero Base Infra visibility until an admin grants it.
- **Device move as privilege escalation.** Cross-Site moves require site-admin on BOTH sides (not just group-admin) precisely because a move can silently rehome a device into a scope where different users have visibility. A malicious group-admin cannot move a device into a group that a colluding accomplice-observer sits on to leak configuration state — because the group-admin can't perform cross-Site moves at all.
- **Grant escalation impossibility.** Site-admin can only grant `role IN ('observer','operator','admin')` within their own Site. They cannot grant `is_system_admin` (that endpoint requires system-admin). Combined with "site-admin cannot delegate group-admin's grant authority", the total power of a compromised site-admin account is bounded to their Site.
- **Delegation is deliberately narrow.** Group-admins **cannot** grant permissions — this is the user's explicit preference for auditability. The trade-off: a site-admin becomes the bottleneck for grant workflows within their Site. The gain: every grant has an obvious "who signed off on this" in the audit log via `role_assignments.created_by_user_id`.
- **Credentials.** No change. Passwords remain AES-encrypted via `secret_service`. Group membership does not affect credential handling.
- **Audit trail.** Every `move_device`, `create_site`, `delete_site`, `create_device_group`, `delete_device_group` must be audited with `user`, `resource`, `resource_id`, and `details` including *both* the source and destination context on move.
- **RLS as backstop** (D12): not required to meet the prompt, but strongly recommended for MSP posture. If added, RLS policies gate by `SET LOCAL app.user_id` set by middleware, and reference `role_assignments` + `users.is_system_admin` (`sites`, `device_groups`, `devices`, `audit_logs` — every scoped table).

---

# 28. Performance Considerations

Post-migration, the hot queries change from "SELECT * FROM devices; then Python filter" to a **JOIN** through Groups. Impact:

- **Device list** — `SELECT ... FROM devices JOIN device_groups ON device.group_id = device_groups.id [JOIN sites ...]`. Add indexes:
  - `devices.device_group_id` (created with the FK).
  - `device_groups.site_id` (already indexed today).
  - `role_assignments.user_id`, `role_assignments.site_id`, `role_assignments.device_group_id` (all indexed per §11.5).
- **Direct device-by-name lookup** — `SELECT ... FROM devices WHERE name = ?` — unchanged; scope check is one extra SELECT.
- **Site inventory view** — `SELECT ... FROM devices WHERE device_group_id IN (SELECT id FROM device_groups WHERE site_id = ?)` — trivially fast at typical sizes.
- **Do not denormalize** by adding a redundant `devices.site_id` alongside `device_group_id`. Per MSP prompt §6, denormalization must be justified. At any realistic MSP scale (<10K devices, <100 sites), the extra join is negligible. Revisit only if a real query becomes slow *and* a JOIN elimination measurably helps.

**Caching**: no new caches introduced. `device_locks` and rate-limit slots continue to key by device *name*; unrelated to hierarchy.

---

# 29. Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Data migration mis-assigns devices where a device was in multiple groups within one Site | Historical group membership lost; audit trail may look inconsistent | Preserve prior state in a JSON blob on an `audit_logs` row per ambiguous device (§24 Step 4). Provide an admin UI/script to re-review. |
| R2 | Removing `devices.site_id` breaks a caller we didn't inventory | Runtime errors in Ansible-driven flows | Grep all references to `d.site_id`/`row.site_id` **before** Phase 4; keep a compatibility read-through (`device.site_id → device.device_group.site_id`) in the `Device` dataclass. |
| R3 | The `effective_role` implementation silently drops a case relative to the legacy `allowed_device_names_for` | Users lose access unexpectedly | Snapshot-diff test (T3.5): for each existing user, compute the current (Python-filter) result set and the new (SQL-JOIN + `role_assignments`) result set; assert equality *before* rollout. The migration in §24 Step 6 must land first so `role_assignments` is populated. |
| R4 | Base-Infrastructure Site conflicts with an existing user-created Site named "Base Infrastructure" | INSERT fails during M2 | Pre-migration check; if a name collision exists, use a system slug like `__base_infrastructure__` or require the operator to rename theirs first. |
| R5 | Frontend and backend deploy independently and drift | Broken Device creation form for a release window | Coordinate deploys; ship the backward-compatible additive endpoints (§19) first, then the breaking ones only once frontend is updated. |
| R6 | The MSP prompt contradicts the prior unimplemented plan (`plan-migracion-modelo-dominio.md`) on Device↔Site cardinality | Rework needed if the user changes their mind mid-implementation | **Get D0 answered before Phase 1.** |
| R7 | Removing group-level M2M loses "device belongs to multiple groups" functionality if that turned out to be a used feature | Feature regression | Audit today's group memberships: `SELECT device_name, COUNT(*) FROM device_group_members GROUP BY device_name HAVING COUNT(*) > 1;`. If the count is > 0, discuss with the user before Phase 4. |
| R8 | Site partial-unique index for Base-Infrastructure differs on SQLite | Dev/test environment quirks | SQLite supports partial indexes; verify syntax works under the SQLAlchemy dialect Alembic emits. Fall back to a Python-level assertion on startup. |
| R9 | Cyclic FK (`sites.default_group_id` ↔ `device_groups.site_id`) trips a downgrade or a test-DB reset | Alembic downgrade fails; test fixtures leak | Use §11.6 Option A (nullable at DB layer, enforce in app); downgrade drops the column before dropping the group. |

---

# 30. Open Questions / Decisions Required

**These are the decisions the user must confirm before Phase 1 starts.** For each: the issue, the options, the trade-offs, and (where the analysis supports one) a recommendation.

## D0 — Reconciling with the prior unimplemented plan
**Issue.** `docs/prompts/new/plan-migracion-modelo-dominio.md` §7 proposed **Device↔Site many-to-many** with "most permissive wins" and a `user_allowed_devices` direct-grant table. The MSP prompt requires **exactly one Site per Device (via one Group)**. These two are mutually exclusive.
**Options.**
- **D0a — MSP prompt supersedes:** discard the many-to-many proposal; proceed with this plan.
- **D0b — Prior plan supersedes:** discard the MSP-hierarchy strictness; adapt this plan to keep M2M.
- **D0c — Hybrid:** keep exactly-one-Group-per-Device, but preserve `user_allowed_devices` as an *authorization* mechanism (a device can still be granted directly to a user regardless of its Site).
**Recommendation.** D0a. The MSP hierarchy the prompt describes is a cleaner domain than a many-to-many with "most permissive wins" (which R7-style admin errors could silently widen access). D0c is workable if the user wants direct grants for exceptional cases.

## D1 — Denormalize `devices.site_id` alongside `device_group_id`?
**Issue.** MSP prompt §6 asks explicitly. **Recommendation.** **No** (per §28). Do not persist `devices.site_id`. Access site via `device.device_group.site`.

## D2 — Base Infrastructure: same `Site` entity or a separate concept?
**Issue.** MSP prompt §15 asks. **Recommendation.** **Same `Site` entity**, distinguished by `SiteModel.kind = 'BASE_INFRASTRUCTURE'`. The DB partial unique index enforces uniqueness; the seed script enforces existence; the service layer enforces undeletability. No separate table.

## D3 — Site identity: numeric `id` or human `slug`?
**Issue.** Today Sites are addressed by numeric id in the API. **Recommendation.** Keep numeric id. Not required by the MSP prompt; a slug is a UX improvement independent of MSP hierarchy.

## D4 — Bootstrap the cyclic Site↔DefaultGroup FK
**Recommendation.** §11.6 Option A: `sites.default_group_id` nullable at DB layer, atomically populated in a single transaction, asserted NOT NULL in the app. Simpler than deferred FKs, works on SQLite.

## D5 — Site deletion semantics
**Issue.** Prompt says Sites can be created/removed per business rules. Today: 409 if devices exist. **Options.**
- Refuse if Site has any Group with any Device (current behavior extended to Groups).
- Cascade: delete Site → delete Groups (empty of Devices).
- Refuse if any non-default Group exists (must delete Groups first).
**Recommendation.** Middle option: `DELETE /sites/{id}` refuses if any Device exists (409); succeeds if only empty Groups (including the Default) remain and *cascades to delete them*. Never cascade to Devices.

## D6 — DeviceGroup name uniqueness: global or per-Site?
**Issue.** Today: globally unique. In MSP with many Sites having a "Default" group, per-Site uniqueness is the sane default. **Recommendation.** **Per-Site.** Change unique constraint to `(site_id, name)`. Migrate legacy globally-unique names by suffixing collisions with `-{site_slug}` if any exist (rare).

## D7 — Can the Default Group be renamed?
**Recommendation.** Yes to renaming (`name` can change); no to deleting (`is_default=TRUE` blocks delete); no to demoting (`is_default` is immutable). Confirms with the user's expected UX.

## D8 — "Remove device from group" semantics
**Issue.** With exactly-one-Group-per-Device, "remove from group" needs to mean "move to Default Group of same Site" (there's no orphan state). **Recommendation.** The API endpoint becomes an alias for `move_to(site.default_group_id)`. Document accordingly.

## D9 — Deprecate the human `name` field?
**Issue.** Diagram shows `name PK`. Today `name` is unique but there's also a surrogate `id`. **Recommendation.** Keep `name` as unique display alias, use `id` internally for FKs. Do **not** deprecate `name` — API URLs already use it.

## D10 — RESOLVED. Per-scope role model is required.
**Confirmed by the user in the design conversation.** Both Group-level permissions AND per-scope roles are first-class requirements. See §10.5, §11.5, §12. Not optional — a single `role_assignments` table with additive/max-role semantics (see D11 for the current rule). Do NOT ship without it.

## D11 — RESOLVED. Grants are additive; max-role wins per scope. (Updated 2026-09-12.)
**Current rule (max-role, additive).** Multiple grants coexist. The effective role at a scope is the **maximum** of every grant that applies to it: a site-wide grant applies to the whole site and every group within it; a group-specific grant applies only when accessing that group. Grants only *elevate* — a lower group-scoped role on a group inside a site the user already admins has **no effect** on that group. Implemented by `VisibilityScope.rol_para()` in `backend/app/models/visibility_scope.py`.

**Special case.** `rol_para(site_id, None)` (site-wide lookup, no group specified) returns the site-wide grant only. A group-scoped admin is deliberately NOT treated as a site-admin — D25 delegation authorization relies on this distinction.

**Historical (superseded).** An earlier version of this rule read *"most-specific-wins"*: a group-scoped grant of any role shadowed the site-wide grant for accesses inside that group, even when it downgraded the effective role. That semantic was reversed on 2026-09-12 because it contradicted operator intuition ("admin on the site" should mean admin everywhere in the site) and could lead to silently-configured downgrades. See `docs/USER_PERMISSIONS_UX_REDESIGN.md` §2.1 for the full rationale and the tests in `backend/tests/test_visibility_scope_unit.py` for the current behavior.

## D12 — Postgres Row-Level Security as defense in depth?
**Recommendation.** Not required by the MSP prompt. Ship without. Revisit if compliance asks demand it.

## D13 — Direct-ID access response: 403 or 404?
**Issue.** Current code returns 403 for out-of-scope. **Recommendation.** Keep 403. Distinguishing "not found" from "not allowed" is fine because IDs are internal to the deployment (no user enumeration risk).

## D14 — Base-Infrastructure default visibility
**Recommendation.** Explicit grant required (safer). Users who previously had access via the `site_id IS NULL` fallback are auto-granted Base Infra during migration Step 6.

## D15 — Device uniqueness key
**Recommendation.** Keep `name`. Optional Phase-6 add: `serial_number`. See §14.

## D16 — Which role can `move` a Device between Groups?
**Recommendation.** `operator` for same-Site moves; `admin` for cross-Site moves.

## D17 — Allow cross-Site Device moves at all?
**Recommendation.** Yes, gated on `admin` (D16). Real MSP use case: moving a customer's device between their sites during a network reorg.

## D18 — Repurpose or delete `/device-groups` frontend page?
**Recommendation.** Repurpose to a per-Site sub-page. Keep the URL as a scoped list. Nesting under `/sites/{id}/groups` is cleaner but a bigger frontend churn.

## D19 — Delete-Group with devices in it: reject or auto-move?
**Recommendation.** Reject with a clear message; require caller to move devices to Default first (or specify `?force=true` for admins that moves them to Default automatically).

## D20 — `PUT /devices/{name}` and `device_group_id`
**Recommendation.** Reject; require `POST /devices/{name}/move`. Keeps the semantics obvious and audit-friendly.

## D21 — Legacy null-site DeviceGroups
**Recommendation.** Delete them in M2 (D21a). They were already invisible to non-admins.

## D22 — Add a `CHANGELOG.md`?
**Recommendation.** Yes — this is the first breaking API change in the project's history. Start the file with the MSP release.

## D_active_job — Reject Device move while it has non-terminal Jobs?
**Recommendation.** Yes. Simpler correctness rule than trying to hand off in-flight state.

## D23 — Grant creation audit trail
**Issue.** Every `role_assignments` row carries `created_by_user_id` (§11.5). Should there also be a per-grant `audit_logs` row?
**Recommendation.** Yes. Every `POST/DELETE /users/{id}/grants` writes an audit event: actor, target user, resource scope, role granted/revoked, timestamp. Redundant with the FK but supports the "who did what to whom, when" narrative queries.

## D24 — Migrating today's `admin` role
**Issue.** Today, `authz.is_unrestricted()` treats `role='admin'` and `role='super-admin'` identically — both bypass site scoping. The new model's `admin` is Site-scoped, not global. Migrating today's admins as *site-admins on all sites* preserves visibility but not privilege (they can no longer create Sites). Migrating them as *system-admins* preserves everything they could do before.
**Options.**
- D24a: Migrate all `admin` and `super-admin` users to `is_system_admin=TRUE` (preserves current behavior; conservative).
- D24b: Migrate `super-admin` → `is_system_admin=TRUE`; `admin` → site-admin `role_assignments` for every existing Site (loses ability to create new Sites, keeps everything else).
- D24c: List all `admin` users at migration time and ask the operator to pick per user.
**Recommendation.** **D24a** for the migration itself, plus a **post-migration audit report** listing every user promoted so the operator can demote where appropriate. Never silently strip privileges — that's the kind of surprise that hides real bugs.

## D25 — RESOLVED. Group-admin cannot delegate.
Confirmed by the user: "I prefer that the site admin delegate those responsibilities to their subordinates" — auditability outweighs the convenience of local delegation. Grant creation/revocation within a Site requires site-admin (or system-admin). Group-admins can operate on devices but cannot mint grants.

## D26 — User profile edits (email, password, activation)
**Issue.** Who can edit a user's profile fields beyond grants?
**Options.**
- D26a: system-admin only.
- D26b: system-admin, OR any site-admin on a Site where the target user has a grant (site-admin manages their "team roster").
- D26c: The user themselves for password/email; system-admin for activation.
**Recommendation.** **D26c** for the target user's own account (self-service password change) + **D26b** for admin-driven edits. Deactivating a user is safer as system-admin-only because it has cross-Site consequences.

## D27 — Audit log visibility
**Issue.** `GET /api/v1/audit` currently returns all audit events to any admin. In the new model, should site-admins only see audit events scoped to their Sites?
**Recommendation.** Yes — a site-admin sees audit events where the audited resource resolves to a Site they admin. System-admins see everything. Requires a JOIN from `audit_logs.resource_id` back to Site — feasible for `device`, `device_group`, `site` resources; for `user` audits, scope to Sites where the target user has a grant.

---

# 31. Proposed Implementation Phases

**Sequenced to preserve `main` green at every phase boundary.** Each phase runs `pytest backend/tests/` + a smoke test in `EXECUTION_MODE=mock` before the next starts.

## Phase 0 — Prerequisites (blocking on user decisions)
- Answer D0 (critical), then D5, D6, D10, D14, D17.
- Snapshot production data structures (Backup Postgres; export SQLite).
- Grep-audit: enumerate all callers of `device_group_members`, `DeviceModel.site_id`, `authz.allowed_device_names_for`, `authz.is_unrestricted`, `authz.set_user_allowed_sites`, `require_role`. Cross-reference against §20.2 and confirm each has a replacement path in the new model.

## Phase 1 — Additive schema (M1)
- Alembic revision adding `sites.kind`, `sites.default_group_id`, `device_groups.is_default`, `devices.device_group_id` — all nullable, no drops.
- Update ORM models to expose the new columns; **no service or API code uses them yet**.
- Bootstrap code path in `main.py` for Base-Infrastructure Site + Default Group (idempotent).
- Non-regression tests must pass with the new columns silent.

## Phase 2 — Backfill (M2)
- Data migration per §24.
- Verification queries must pass (§23 M2 end).
- New tests under `backend/tests/migrations/`.

## Phase 3 — Application cutover (behind a feature flag)
- Add `is_system_admin` bool to `users` (nullable during transition, defaulted from `role IN ('admin','super-admin')`).
- Create `role_assignments` table (still empty for restricted users at this point).
- Introduce `core/authz.py` `effective_role` implementation alongside the existing helpers. Guard the switch with `MSP_STRICT_HIERARCHY` env flag (default `false`).
- Introduce `core/scope.py` `require_scope(...)` dependency; when flag is false, it delegates to old `authz.ensure_device_allowed` for continuity.
- Add `POST /devices/{name}/move` + `GET /sites/{id}/groups` (always on; additive).
- Add `POST/DELETE/GET /users/{id}/grants` and `PUT /users/{id}/system-admin` endpoints (writes rows in the new table).
- Snapshot-diff test (R3): assert `MSP_STRICT_HIERARCHY=true` returns the same visible-devices set as `false` for every existing user. Run this after the §24 Step 6 migration has populated `role_assignments`.

## Phase 4 — Enforcement (M3) + frontend rollover
- M3: NOT NULL constraints, partial unique indexes, FK ondelete tightening.
- Flip `MSP_STRICT_HIERARCHY=true` in defaults.
- Update `POST /api/v1/devices` schema: `site_id` becomes required; add `device_group_id`.
- Update `PUT /api/v1/devices/{name}`: drop `site_id`.
- Deprecate `POST /device-groups/{id}/members`, `DELETE /device-groups/{id}/members/{device_name}` (return 200 with a Deprecation header for one release, forwarding to `move`).
- Update `GET /api/v1/sites` to filter by scope (fix G10).
- Frontend: update device create/edit forms; add Move action; add Site detail page.

## Phase 5 — Cleanup (M4)
- Alembic drops `devices.site_id`, `device_group_members`, `user_allowed_sites`, `users.role`.
- Delete deprecated endpoints (`PUT /users/{id}/allowed-sites` compatibility shim; `POST /device-groups/{id}/members`; `DELETE /device-groups/{id}/members/{device_name}`).
- Delete `add_member`, `remove_member` service functions. Delete `DeviceGroupMemberModel`, `UserAllowedSiteModel`.
- Delete legacy `authz.py` helpers (`is_unrestricted`, `allowed_site_ids_for`, `allowed_device_names_for`, `set_user_allowed_sites`, `get_user_allowed_sites`).
- Delete the `site_id IS NULL` fallback branch.
- Regenerate `docs/prompts/new/diagrama_clases_dominio.drawio` reflecting the target model in §10.
- Remove the `MSP_STRICT_HIERARCHY` flag from settings and its guarded branches.

## Phase 6 (optional) — Device uniqueness key + audit scoping
- If D15 evolves: add `devices.serial_number` (nullable, unique when not null); backfill via discovery scan (out of scope for this plan).
- If D27 confirmed: scope `GET /api/v1/audit` results by the requester's grants (system-admin sees all; site-admin sees only their Site's audit events).
- Optional: Postgres RLS as defense-in-depth (D12).

---

# 32. Detailed Task Breakdown

Grouped by phase, using the format required by MSP prompt §30. **Paths marked "TBD" require inspection of the exact caller site at implementation time.**

## Phase 1 tasks

### T1.1 — Add Alembic revision "M1"
- **File:** `backend/migrations/versions/<new>_msp_additive.py`
- **Change:** upgrade adds `sites.kind`, `sites.default_group_id`, `device_groups.is_default`, `devices.device_group_id` (all nullable, no drops); downgrade reverses.
- **Reason:** enables target columns without breaking any current caller.
- **Dependencies:** Alembic head = `d8a5f2c1b630`.
- **Tests:** `backend/tests/test_migrations_msp_m1.py` — upgrade + downgrade round-trip on a seeded DB.

### T1.2 — Extend ORM models
- **File:** `backend/app/db/models.py`
- **Change:** `SiteModel.kind`, `SiteModel.default_group_id` (nullable FK to `device_groups.id`, `use_alter=True`); `DeviceGroupModel.is_default`; `DeviceModel.device_group_id` (nullable FK, `use_alter=True`). Add corresponding relationships.
- **Reason:** ORM must know about columns before Phase 2/3 code can populate them.
- **Impact:** none behaviorally; add-only.
- **Tests:** existing model tests re-run; add `test_msp_models_have_new_columns.py`.

### T1.3 — Base-Infrastructure bootstrap
- **File:** `backend/app/main.py` (extend the FastAPI startup event that seeds defaults; path to confirm — likely `main.py` `@app.on_event("startup")` or `lifespan`).
- **Change:** on startup, call `site_service.ensure_base_infrastructure()` (new function in `site_service.py`) that atomically creates the Site (if missing) and its Default Group (if missing) and sets `default_group_id`.
- **Reason:** required precondition for Phase 2 backfill.
- **Tests:** `test_bootstrap_creates_base_infrastructure.py` — start app twice, assert exactly one row `kind=BASE_INFRASTRUCTURE`.

## Phase 2 tasks

### T2.1 — Add Alembic revision "M2" (data migration)
- **File:** `backend/migrations/versions/<new>_msp_backfill.py`
- **Change:** SQL per §24 (Steps 1–6). Include verification `SELECT` blocks that fail the migration on any violation.
- **Reason:** populate the new columns for existing data.
- **Tests:** `test_migrations_msp_m2_creates_default_groups.py`, `test_migrations_msp_m2_backfills_device_groups.py`, `test_migrations_msp_m2_handles_ambiguous_devices.py`.

### T2.2 — Snapshot-diff regression test
- **File:** `backend/tests/test_authz_snapshot_diff.py` (new)
- **Change:** for every user in the seed DB, compute `old_visible_devices` (Python-filter) and `new_visible_devices` (SQL JOIN); assert equality.
- **Reason:** R3.
- **Dependencies:** T2.1 must have run in the fixture.

## Phase 3 tasks

### T3.1 — Introduce per-scope authz alongside legacy (behind flag)
- **Files:** `backend/app/core/authz.py` (extend, don't replace yet); `backend/app/services/effective_role.py` (new); `backend/app/services/role_assignment_service.py` (new); `backend/app/core/scope.py` (new — `require_scope`, `require_system_admin`).
- **Change:** implement `effective_role(user, resource_type, resource_id)` per §10.5. Wire it into a new `require_scope(operation_name)` dependency. Gate all new code paths behind `settings.MSP_STRICT_HIERARCHY` (default false). Old `authz.py` functions remain intact.
- **Reason:** allow side-by-side operation for the cutover, snapshot-diff test in T3.5.
- **Dependencies:** T1.2 (`role_assignments` model), T2.3 (backfilled data).
- **Tests:** unit tests for `effective_role` covering Juan/Alice/Bob scenarios (§26.2).

### T3.2 — Rewrite list endpoints
- **Files:** `backend/app/api/devices.py:33-38`, `api/device_groups.py:44-50`, `api/sites.py:40-42`.
- **Change:** replace post-fetch filter with `inventory.list(user=...)`, `device_group_service.list(user=...)`, `site_service.list(user=...)`. Each service method runs the corresponding query from §12.4 (a JOIN through `role_assignments`).
- **Reason:** G8, G10.
- **Tests:** `test_devices_list_scope.py`, `test_groups_list_scope.py`, `test_sites_list_scope.py`.

### T3.3a — Grant management endpoints
- **Files:** `backend/app/api/users.py` (extend); `backend/app/services/role_assignment_service.py`; `backend/app/schemas/role_assignment.py`.
- **Change:** implement `POST /users/{id}/grants`, `DELETE /users/{id}/grants/{grant_id}`, `GET /users/{id}/grants`, `PUT /users/{id}/system-admin`. Auth per §12.3 matrix. Every mutation writes an audit event (D23).
- **Dependencies:** T1.2, T3.1.
- **Tests:** `test_grants_endpoint_creates_row.py`, `test_grants_endpoint_rejects_out_of_scope_site_admin.py`, `test_grants_endpoint_rejects_system_admin_grant_from_non_system_admin.py`, `test_system_admin_endpoint_prevents_last_admin_demotion.py`.

### T3.3b — Compatibility shim for `PUT /users/{id}/allowed-sites`
- **File:** `backend/app/api/users.py`.
- **Change:** the existing endpoint stays for one release. Body `{allowed_site_ids: [1, 2, 3]}` now (1) deletes all `role_assignments` for this user at Site scope where `device_group_id IS NULL`, (2) creates observer-role Site grants for the supplied IDs. Response header `Deprecation: true`.
- **Reason:** external clients continue working for one release.
- **Tests:** `test_legacy_put_allowed_sites_creates_observer_grants.py`.

### T3.4 — Move + Site-groups endpoints
- **Files:** `backend/app/api/devices.py`, `api/sites.py`.
- **Change:**
  - `POST /api/v1/devices/{name}/move` — body `{device_group_id}`. Auth: enforced by `require_scope('move_device')` — checks admin on source AND target group; if cross-Site, escalates to site-admin on both.
  - `GET /api/v1/sites/{id}/groups` — auth: `require_scope('list_site_groups')` — observer or higher on Site or any Group.
- **Tests:** `test_device_move.py`, `test_site_groups_listing.py`, plus the operation-matrix tests in §26.2.

### T3.5 — Snapshot-diff regression test
- **File:** `backend/tests/test_authz_snapshot_diff.py` (new, from Phase 2 stub — expand).
- **Change:** for each user in a seeded DB with `MSP_STRICT_HIERARCHY=true`, assert `effective_role(user, device)` computed via `role_assignments` yields the same visible-devices set as the legacy `allowed_device_names_for(user)` computed via `user_allowed_sites`.
- **Reason:** R3.

## Phase 4 tasks

### T4.1 — Alembic revision "M3" (enforce)
- **File:** `backend/migrations/versions/<new>_msp_enforce.py`
- **Change:** `NOT NULL` on `devices.device_group_id`; `NOT NULL` on `device_groups.site_id`; partial unique indexes; FK ondelete → RESTRICT.
- **Tests:** `test_migrations_msp_m3_enforces_constraints.py`.

### T4.2 — Schema and endpoint breaking changes
- **Files:** `backend/app/schemas/device.py`, `api/devices.py`.
- **Change:** `DeviceCreate.site_id: int` (required); `DeviceCreate.device_group_id: Optional[int]`; `DeviceUpdate` drops `site_id`; `PUT /devices/{name}` rejects `site_id` in body.
- **Reason:** enforce the invariant (G1, G2, G3, G6, G11).
- **Tests:** update `test_devices_create.py`, `test_devices_update.py`.

### T4.3 — Deprecate M2M endpoints
- **File:** `backend/app/api/device_groups.py`
- **Change:** `POST /{id}/members`, `DELETE /{id}/members/{device_name}` respond 200 with `Deprecation: true` header and internally call `inventory.move(device_name, target_group_id or default)`.
- **Reason:** compatibility for one release.
- **Tests:** `test_deprecated_member_endpoints_still_work.py`.

### T4.4 — Frontend cutover
- **Files:** `frontend/src/app/(dashboard)/devices/page.tsx`, `.../device-groups/page.tsx`, `.../sites/page.tsx`, `frontend/src/services/api.ts`, `frontend/src/types/device.ts`.
- **Change:** per §21.
- **Tests:** frontend has no unit-test infra; verify manually per §26 checklist + Cypress smoke if introduced.

### T4.5 — Flip `MSP_STRICT_HIERARCHY` default to `true`
- **File:** `backend/app/core/config.py` (path to confirm).
- **Change:** default flip; keep the flag as an escape hatch for one release.

## Phase 5 tasks

### T5.1 — Alembic revision "M4"
- **File:** `backend/migrations/versions/<new>_msp_cleanup.py`
- **Change:** `ALTER TABLE devices DROP COLUMN site_id;` `DROP TABLE device_group_members;`.
- **Reason:** finalize.
- **Tests:** `test_migrations_msp_m4_drops_legacy.py`.

### T5.2 — Code cleanup
- **Files:** `backend/app/services/device_group_service.py` (delete `add_member`, `remove_member`), `db/models.py` (delete `DeviceGroupMemberModel`), `api/device_groups.py` (delete deprecated routes), `core/authz.py` (delete `site_id IS NULL` fallback branch).
- **Reason:** remove dead code.
- **Tests:** deleted tests, no new ones.

### T5.3 — Regenerate diagram
- **File:** `docs/prompts/new/diagrama_clases_dominio.drawio` (or a new versioned file `docs/msp/diagrama_clases_msp.drawio`).
- **Change:** reflect the model in §10 and §10.5. Remove the `device_sites` line and the orange `Usuario ···› Device` "acceso directo" line (superseded). Add `Site.default_group_id`, `Site.kind`, `DeviceGroup.is_default`. Replace `Usuario.allowed_sites: int[]` with a new `RoleAssignment` class linked to both `Usuario` and `Site`/`DeviceGroup`.

## Phase 6 tasks (optional)

(Phase 6 tasks superseded — D10/D11 are RESOLVED. The `role_assignments` model already lands in Phases 1–4 as a first-class requirement. Any remaining optional work is captured in Phase 6 above.)

---

# 33. Recommended Implementation Order

1. **User answers** D0, D5, D6, D14, D17, D19, D24, D26, D27 (§30). D10, D11, D25 are already RESOLVED in-plan.
2. **Phase 0** grep-audit + backups.
3. **Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5**, in that order, with the release-boundary gates called out in §31.
4. **Phase 6** only after Phase 5 has soaked in production for at least one release cycle.

At every phase boundary:
- All existing `backend/tests/` pass.
- New tests specified in §26 for that phase pass.
- Smoke test in `EXECUTION_MODE=mock` covers: create Site → default group appears → create Device → device shows in Global Inventory scoped to the caller → move Device to another Group.
- Frontend manual smoke of the two paths above.

---

## Appendix A — Traceability of the Domain-Model Invariants

For each invariant asserted by the MSP prompt (§11), the mechanism that enforces it in the target design:

| Invariant | Mechanism |
|---|---|
| DeviceGroup without Site is impossible | `device_groups.site_id NOT NULL` (M3); ORM constructor requires it; API schema requires it |
| Device without DeviceGroup is impossible | `devices.device_group_id NOT NULL` (M3); ORM constructor requires it; API defaults to Site's Default Group when omitted |
| Device belonging to multiple DeviceGroups is impossible | Only one column `device_group_id`; `device_group_members` table removed |
| Device belonging to multiple Sites is impossible | Direct consequence of the above two; enforced structurally |
| Every Site has a Default Group | Site-creation transaction (§15); startup bootstrap for legacy data; partial unique index prevents multiple defaults per Site |
| Exactly one Base-Infrastructure Site | Partial unique index `ux_sites_single_base_infra`; seed step enforces existence |
| Base-Infrastructure Site cannot be deleted | Application-layer check in `site_service.delete_site` |
| Cross-Site device move requires admin | Application-layer check in `POST /devices/{name}/move` (D16) |

---

*End of MSP Implementation Plan.*

*Nothing in this document should be implemented until Section 30 decisions are confirmed.*
