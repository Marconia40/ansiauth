# User & Permissions UX Redesign

> **Status:** Design, not yet implemented. Companion to `docs/MSP_IMPLEMENTATION_PLAN.md` (Phase 5+).
> **Scope:** Frontend UX + one small backend semantic change. No schema migrations required.
> **Origin:** Design conversation 2026-09-12. Confirmed by user before implementation.

---

## 1. Goals

The Phase-5 backend already supports per-scope grants (`role_assignments` table, `VisibilityScope`, D25 delegation rules). The **UI does not surface that model correctly**, so operators see a flat/global abstraction that misrepresents the actual permissions state. This redesign makes the UX match the backend model, and tightens one semantic rule (max-role instead of most-specific-wins).

Concrete pain points being addressed:

1. `System-admin` toggle looks like a per-site button but is global.
2. The `Role` column shows a synthesized global role, hiding real per-site grants.
3. The `Edit` action opens a role dropdown that does nothing (dead UI).
4. `Grants…` lives in a separate button; system-admin and grants are managed in unrelated flows.
5. `Create user` mixes system-admin choice with the create form; observers/operators are silently created without any access at all.
6. No way to answer "who has access to Site X?" without opening each user's grants modal.
7. `most-specific-wins` in `VisibilityScope.rol_para()` can silently downgrade a site-admin on a specific group.
8. No global scope-switcher: system-admins and multi-site users always see everything at once.

---

## 2. Backend changes

### 2.1 Semantic: switch role resolution from **most-specific-wins** to **max-role**

**File:** `backend/app/models/visibility_scope.py` — method `rol_para(site_id, device_group_id)`.

**Current behavior:** returns the *most specific* matching grant. If a user has `admin` on Site-A and `observer` on Site-A/Group-3, they get `observer` inside Group-3.

**New behavior:** returns the **maximum** role across all applicable grants (using the existing hierarchy `observer=1 < operator=2 < admin=3`). Same example → `admin` inside Group-3. Semantics become **additive**: a grant can only *elevate*, never downgrade.

**Rationale:**

- Matches operator intuition: "admin on the site" means admin everywhere in the site.
- Prevents silently-configured downgrades.
- The useful inverse case (`observer` on site + `admin` on specific group = "read-only overall, edit this one group") **still works** under max-role.

**Impact:**

- `backend/tests/test_msp_effective_role.py` — update tests that assert most-specific-wins semantics.
- `docs/MSP_IMPLEMENTATION_PLAN.md` — amend the decision that codified most-specific-wins (find and update the D-numbered rule; add a note pointing here).

### 2.2 (Optional) `GET /users?site_id=X` filter

**File:** `backend/app/api/users.py`.

Add an optional `site_id` query param that filters the returned users to those with at least one grant on that site. Enforced inside the existing `VisibilityScope` — requesting a `site_id` the caller cannot see returns 403.

Purpose: powers the topbar scope switcher's per-site users view. Not strictly required for MVP (frontend can filter client-side), but preferred for pagination/perf.

### 2.3 No schema changes

Everything else — `role_assignments` table, `is_system_admin` flag, grant/revoke endpoints, D25 authorization — stays as-is.

---

## 3. Frontend changes

### 3.1 New: `ScopeContext` + topbar scope switcher

**New files:**

- `frontend/src/context/ScopeContext.tsx` — React context holding `selectedScope: 'ALL' | { siteId: number }`. Persisted in URL query param (`?scope=all` or `?scope=site:12`) so links are shareable; fallback to `localStorage` for the last-picked value when no query param is present.
- `frontend/src/components/ScopeSwitcher.tsx` — dropdown in the topbar.

**Content of the dropdown**, computed from `AuthContext` and the sites API:

| User's grants | Dropdown items |
|---|---|
| `is_system_admin=True` | `All sites` + every site in the system |
| Grants on Site-A only | Fixed label `Site A` (no dropdown, shown as read-only badge) |
| Grants on Site-A and Site-B | `All (accessible)` + `Site A` + `Site B` |

**Behavior contract:**

- Every list page (`/users`, `/devices`, `/device-groups`, `/vlans`, …) reads the active scope and applies it as a filter on top of the visible scope.
- The sidebar collapses its tree to the active scope. When scope is `Site-A`, sidebar shows `Organization → Site-A → groups`. When scope is `ALL`, it shows the full tree the user can see.
- Switching scope updates the URL without a full navigation.

### 3.2 New: `RequireScopedRole` replaces `RequireRole`

**Current:** `RequireRole('admin')` checks only the global synthesized role from the JWT.

**New:** `RequireScopedRole(op)` — takes an operation from `OP_MIN_ROLE` (mirror the backend matrix in a shared TS constant), and checks whether the current user has sufficient role for that operation in the active scope. Uses the same "max-role" resolution as the backend.

**Files:**

- `frontend/src/components/RequireScopedRole.tsx` (new).
- `frontend/src/constants/opMinRole.ts` (new) — TypeScript mirror of `backend/app/core/scope.py:OP_MIN_ROLE`. Reason: avoid a round-trip for every gate. Documented rule: whenever the backend matrix changes, this file must be updated in the same PR.
- Replace all call sites of the old `RequireRole` with the new one.

### 3.3 Unified `Manage user` modal (replaces separate Edit / Grants / System-admin)

**File:** `frontend/src/app/(dashboard)/users/page.tsx` — major rewrite of the row actions and modal.

**Row actions become just two buttons:** `Edit` and `Delete`. The `Grants…` button and the `System-admin: Yes/No` column button both **go away**. Everything moves into the `Edit` modal.

**Modal structure — `Manage user: {username}`:**

- **Section 1: Credentials**
  - `Username` (read-only after creation).
  - `Email` (editable).
  - `Reset password` (optional field; blank = no change).

- **Section 2: System-wide access** (only visible if the viewer is `is_system_admin=True`)
  - Toggle: `Grant system-wide admin`.
  - Caption in English: `Bypasses every per-site and per-group permission. Only for platform operators.`
  - Guard: cannot turn off for the last active system-admin (backend already enforces; frontend disables + tooltip).

- **Section 3: Site & group grants**
  - Table of current grants: `Site | Group | Role | Revoke`.
  - "Add grant" row: site dropdown → group dropdown (or `Whole site (any group)`) → role dropdown → `Add`.
  - **Guardrail (new, tied to §2.1 max-role):** if the target user already has an `admin` grant at site level for the site being edited, group-level grants below `admin` for groups inside that site are visually disabled with tooltip `This user is already admin on the whole site — a lower role on a group has no effect.`
  - Site/group dropdowns filtered to what the viewer can delegate (D25 mirror; backend still enforces).

- **Section 4 (footer):** `Save changes` / `Cancel`.

### 3.4 Two-step `Create user` flow

**Step 1 — `Create user` modal:**

- Fields: `Username`, `Password`, `Email` (optional).
- On submit: create user with `is_system_admin=False` and no grants.
- Button: `Create and configure access →`.

**Step 2 — reuses the `Manage user` modal** from §3.3, opened automatically with the just-created user.

- Section 2 (system-wide) hidden entirely for site-admin viewers (not just disabled — no visible affordance).
- `Skip (user has no access yet)` closes the modal without further action. A user without grants is a valid state; they simply cannot see anything after logging in. The Edit action from the users list can complete their access later.

**Failure mode:** if Step 2 fails or is skipped, the user exists but has no permissions. This is intentional and recoverable — no need for a transactional `POST /users/with-grants` endpoint.

### 3.5 Users list: replace `Role` column with grant summary

**Current columns:** `Username | Role | System-admin | Actions`.

**New columns:** `Username | Access | Actions`.

The `Access` cell renders:

- If `is_system_admin=True` → single badge `SYSTEM-WIDE ADMIN` (danger color).
- Otherwise → up to 3 badges of the form `Site A: admin` / `Site B: observer` / `Site C/Group-1: operator`. If more grants exist, append `+N more` clickable → opens the manage-user modal at Section 3.
- If no grants and not system-admin → muted text `No access`.

### 3.6 New page/tab: `Users of {Site X}` (via scope switcher)

No new route needed. When the active scope is `Site X`, the existing `/users` page:

- Filters the list to users with at least one grant on Site X.
- The `Access` column collapses to just the roles on Site X and its groups (other sites hidden while scoped).
- `Create user` in this scope pre-selects Site X in Step 2's grant form.

When the active scope is `ALL`, the page behaves as it does today (full user list, all grants shown).

### 3.7 Sidebar filtering

**File:** wherever the sidebar tree is rendered (likely `frontend/src/components/Sidebar.tsx` — confirm at implementation time).

Sidebar reads the active scope from `ScopeContext`. When scoped to a single site, the tree only renders that site and its groups. When scoped to `ALL`, renders every site the user can see.

---

## 4. UI copy (English, final)

- Topbar switcher default label: `Viewing: All sites` / `Viewing: {Site name}`.
- System-admin toggle caption: `Grant system-wide admin — bypasses every per-site and per-group permission. Only for platform operators.`
- Grants section header: `Site & group grants`.
- Grants section caption: `A grant is one role at one scope (a whole site, or a specific group within a site). Grants only elevate — a lower role on a group inside a site the user already admins has no effect.`
- Whole-site option in group dropdown: `Whole site (all current and future groups)`.
- Skip button in Create Step 2: `Skip — user has no access yet`.
- Empty access badge: `No access`.
- System-wide badge: `SYSTEM-WIDE ADMIN`.

---

## 5. Implementation order

Ordered by risk (low → higher) and dependency. Each item is independently mergeable.

1. **[Backend]** §2.1 — flip `VisibilityScope.rol_para()` to max-role. Update tests. Amend MSP plan doc.
2. **[Frontend cleanup]** §3.5 — replace `Role` column with `Access` badges. Removes dead UI first, low risk.
3. **[Frontend cleanup]** §3.3 — unify `Manage user` modal; remove separate `Grants…` button and `System-admin` column. Reuses existing APIs.
4. **[Frontend]** §3.4 — split `Create user` into two steps, reusing the modal from step 3.
5. **[Frontend infra]** §3.1 — `ScopeContext` + topbar switcher, initially wired only to `/users` filtering.
6. **[Frontend]** §3.7 — sidebar respects active scope.
7. **[Frontend]** §3.2 — introduce `RequireScopedRole`; migrate call sites.
8. **[Backend, optional]** §2.2 — `GET /users?site_id=X` query param.
9. **[Frontend]** §3.6 — scope-aware users page + `Create user` prefill.

Steps 1–4 deliver 80% of the perceived UX improvement without touching routing or context. Steps 5+ complete the MSP feel.

---

## 6. Testing plan

**Backend:**

- New unit test cases for `rol_para()`:
  - `admin@site + observer@site/group` → `admin` inside group (was `observer`).
  - `observer@site + admin@site/group` → `admin` inside group (unchanged).
  - `operator@site + admin@site/group-1 + observer@site/group-2` → `admin` on group-1, `operator` on group-2 (was `observer` on group-2 under old rule).
- Regression: existing D25 grant/revoke tests continue to pass unchanged.
- If §2.2 implemented: `GET /users?site_id=X` returns 403 for a site the caller cannot see, returns filtered list otherwise.

**Frontend:**

- Manual golden-path for each modal state (Create Step 1 → Step 2 with system-admin viewer, Create Step 2 with site-admin viewer, Edit with both grant types, guardrail for admin-on-site + group grant).
- Manual verification that the `Access` column reflects real grants, not the synthesized global role.
- Scope switcher: confirm URL param persistence, sidebar collapse, and per-page filtering for at least `/users` and `/devices`.
- `RequireScopedRole` gate: viewer with `admin` on Site-A but no grants elsewhere can open `/users` when scoped to Site-A and gets denied when scoped to Site-B.

---

## 7. Risks and open questions

**R1. Behavior change for existing deployments.** If any deployment relies on most-specific-wins to downgrade (§2.1), that will break. Confirmed with user: this is intentional. Add a changelog entry.

**R2. Duplicated authorization matrix.** §3.2 introduces a TS copy of `OP_MIN_ROLE`. Ownership rule: backend is the source of truth; frontend copy is a UI hint only. All real enforcement stays in `require_scope()` server-side. A CI check (`scripts/verify_op_min_role_parity.py`) is a nice-to-have but not required for MVP.

**R3. Modal size.** The unified `Manage user` modal is dense. If it grows past ~2 screens, consider tab-splitting (`Credentials | Access`) rather than sections.

**R4. Legacy super-admin created via old dropdown.** Users created via the current form with `role=super-admin` mapped to `is_system_admin=True`. That state is preserved; no migration needed.

**Open Q1:** Should the scope switcher persist per-tab (URL) or per-session (localStorage), or both? Current proposal: URL primary, localStorage as fallback. Confirm at implementation time.

**Open Q2:** When a site-admin creates a user and skips Step 2, that user has zero grants. Do we want a warning banner on the users list flagging "N users have no access"? Not in scope for this doc, but worth revisiting once the flow lands.
