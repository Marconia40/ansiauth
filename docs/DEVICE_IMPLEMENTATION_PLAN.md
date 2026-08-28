# Device Implementation Plan

> **Status:** Analysis and design only. **No code has been modified.**
> **Prompt of origin:** `docs/plan-migracion-modelo-dominio.md` §3 ("`Device` — el
> cambio más grande") and §8 (Fase 1), as designed in conversation, retargeted against
> the current repository state.
> **Reference diagram:** `diagrama_clases_dominio.drawio` (26 classes) — `Device`,
> `Inventory`, the four `Base*Driver` hierarchies, `Orquestador`, `Job`/`GroupJob`.
> **Author role:** produced as the same kind of senior-architect analysis as
> `docs/MSP_IMPLEMENTATION_PLAN.md`, against branch `refactor/MSP-phase-6`, HEAD as of
> `2026-08-22`.

---

## Reading guide

Same structure as `MSP_IMPLEMENTATION_PLAN.md`, scoped to one class instead of the
whole authorization/hierarchy model:

- **Sections 1–5** describe the **current** state — including a real finding: MSP's
  own Phase 3 already built an `Inventory` class, and a corrected finding: this plan
  does not extend it, because there's no Repository-pattern precedent to justify that.
- **Sections 6–8** describe the **target** state (`Device` composing its own vendor
  drivers, lazily).
- **Sections 9–11** describe **the work**, per layer.
- **Section 12** lists **explicit non-goals**.
- **Section 13** captures **decisions the user must confirm** before implementation.
- **Section 14** gives the **phased task breakdown**.

Nothing in Sections 9–14 should be started until Section 13 is resolved.

---

# 1. Executive Summary

`ansiauth` has a fully working VLAN/Port automation stack — retry, rollback, Celery
dispatch, audit, vendor drivers — but **none of it is reachable through the `Device`
object**. `Device` (`backend/app/models/device.py`) is a 10-field dataclass with zero
methods. Every operation lives as a free function, split across `vlan_service.py`
(5 near-identical functions repeating the same `_resolve_device → decrypt_password →
_get_driver → driver.method(...)` dance) and its Port equivalent.

This plan turns `Device` into the rich object the reference diagram shows —
`device.create_vlan(vlan)`, `device.configure_port(port)`, etc. — **without**
duplicating any of the retry/lock/rollback machinery, which stays exactly where it is
(`orchestration_runner.run_operation`, Celery task wrappers).

**What changed since this was first scoped, and why it matters:**

- MSP's Phase 3 (`refactor/MSP-phase-6`, `backend/app/services/inventory_service.py`)
  already built a real `Inventory` **class** — `get`, `list`, `register`, `move`,
  `deregister` — as "the sole entry point for Device CRUD". An earlier draft of this
  plan had `Inventory.get()` resolve and attach vendor drivers to the `Device` it
  returns. **That draft is superseded** — verified there is no Repository-pattern
  precedent anywhere in this codebase (`git grep -i repository` returns nothing;
  `device_service.py`/`inventory_service.py` touch SQLAlchemy directly, there's no
  abstraction layer whose job is "hydrate a fully-wired domain object"). `Inventory`
  is **not touched at all** by this plan — `Device` resolves its own drivers, lazily,
  on first use (§6).
- `Device` already gained `device_group_id` / `device_group_name` fields (MSP). The
  domain-model work here is additive to that, not conflicting.
- The old `authz.ensure_devices_allowed` is **gone** — MSP deleted `core/authz.py`
  entirely and replaced it with `effective_role(session, user, "device", name)`
  (`core/scope.py`, `core/effective_role.py`). Any endpoint-layer authorization check
  in this plan targets that function, not the old one.
- `vlan_service.py`, `vlan_execution_service.py`, `port_execution_service.py`,
  `orchestration_runner.py`, `vendors/`, `worker.py` (Celery) are **verified unchanged**
  across every MSP phase (`git diff --stat` between `MSP-phase-0-and-1` and
  `MSP-phase-6` returns empty for all of these). This is the strongest possible
  confirmation that the Device work is orthogonal to MSP and can proceed independently.

---

# 2. Current Architecture (the slice that matters here)

| Layer | Path | Role |
|---|---|---|
| API routers | `app/api/vlans.py`, `app/api/ports.py` | Validate, check `effective_role` per device (`_authz_devices` helper, new since MSP), delegate to `*_execution_service.enqueue_*` |
| Execution services | `app/services/vlan_execution_service.py`, `port_execution_service.py` | `enqueue_*` functions: create `Job`/`GroupJob` rows, capture pre-state, dispatch Celery task via `.delay()` |
| Orchestration | `app/services/orchestration_runner.py` | `run_operation()` — the generic lock/rate-limit/pre-state/retry/rollback/audit skeleton. Domain-agnostic. |
| Read/write dispatch | `app/services/vlan_service.py`, (Port has no equivalent single file — logic lives inline in `port_execution_service.py`/`port_config_service.py`) | Resolves device row → decrypts password → resolves vendor driver → calls it |
| Vendor drivers | `app/services/vendors/{huawei,cisco}/{vlan_driver,port_driver}.py` | `BaseVendorDriver`/`BasePortDriver` + 2 concrete implementations each |
| Domain dataclass | `app/models/device.py` | `Device` — **anemic**, see §3 |
| Application service | `app/services/inventory_service.py` | `Inventory` class — **exists now**, thin CRUD/list/move wrapper, see §4 |
| Celery | `app/worker.py` | Task registry; `enqueue_*` calls `.delay()` on wrappers defined in the execution-service modules |

---

# 3. Current `Device` Domain Model

```python
# backend/app/models/device.py — verbatim, current state
@dataclass
class Device:
    name: str
    host: str
    vendor: str
    username: str
    encrypted_password: str
    platform: str = "ios"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    site_id: Optional[int] = None
    site_name: Optional[str] = None
    device_group_id: Optional[int] = None       # MSP Phase 3
    device_group_name: Optional[int] = None      # MSP Phase 3
```

Zero methods. Every "thing you can do to a device" is a free function elsewhere,
repeating the same shape:

```python
# vlan_service.py — one of five near-identical functions
def create_vlan_on_device(vlan_id: int, name: str, device_id: str) -> dict:
    if EXECUTION_MODE == "mock":
        return _mock_create_vlan(vlan_id, device_id)
    dev = _resolve_device(device_id)                      # re-fetches from DB
    pw = secret_service.decrypt_password(dev.encrypted_password)
    driver = _get_driver(dev)                              # re-resolves the vendor driver
    return driver.create_vlan(vlan_id, name, dev, pw)
```

`delete_vlan`, `update_vlan_description`, `get_vlans`, `save_config_on_device` all
repeat this exact four-step dance. Port operations repeat the same pattern again in
`port_execution_service.py`/`port_config_service.py`, resolving the driver fresh on
every single call.

---

# 4. Current `Inventory` (MSP Phase 3 — already built, verified)

```python
# backend/app/services/inventory_service.py — verbatim, current state (excerpted)
class Inventory:
    """Sole entry point for Device CRUD, listing, and movement."""

    def __init__(self, db=None):
        self._db = db  # currently unused — every method opens its own get_session()

    def get(self, name: str) -> Optional[Device]:
        from app.services import device_service
        return device_service.get_device(name)

    def list(self, user: dict, *, site_id=None, device_group_id=None) -> list[Device]:
        ...  # visibility via role_assignments, unioned site+group grants

    def register(self, *, name, host, vendor, platform, username, password,
                 site_id, device_group_id, actor) -> Device: ...

    def move(self, ...): ...
    def deregister(self, name: str, actor: dict) -> None: ...
```

**This is the class our earlier design called `Inventory` in the drawio diagram.**
It already exists, already has the right shape (`get`, `list`, `register`/`alta_device`,
`move`), and its own docstring caps its scope at exactly 5 methods "by the phase-3
acceptance criteria."

**This plan does not touch `Inventory` at all — not its method count, not `get()`'s
body, nothing.** `Inventory.get()` stays a one-line delegate exactly as it is today.
There's no Repository-pattern convention in this codebase that would justify making
"the thing that fetches a `Device`" also responsible for wiring its runtime
collaborators (verified — see §1). `Device` resolves its own drivers instead (§6).

---

# 5. Gaps

| # | Gap | Current | Target |
|---|---|---|---|
| DG1 | `Device` has no behavior | Pure dataclass | Composes 2 vendor drivers (VLAN, Port); exposes `create_vlan`, `configure_port`, etc. |
| DG2 | Driver resolved on every call | `_get_driver(dev)` re-runs `vendors.dispatcher` + password decrypt each time | Resolved once per `Device` instance, cached on first use (lazy, self-resolved — §6) |
| DG3 | 5 near-identical functions in `vlan_service.py` | Copy-pasted resolve/decrypt/dispatch | Collapsed into 5 one-line `Device` methods delegating to the *same* driver calls |
| DG4 | Port logic has no single-file equivalent to `vlan_service.py` | Split across `port_execution_service.py` + `port_config_service.py` | Same collapse, mirrored for Port |
| DG5 | Testing depends on `monkeypatch.setattr("app.api.ports._capture_pre_state_...")` (string path) | Fragile — string lookup, no static check | New tests use constructor injection (`Device(..., vlan_driver=FakeVlanDriver())`) |
| DG6 | Naming convention mismatch | Earlier design pass (chat + `.drawio`) used Spanish method names (`crear_vlan`); 100% of real code (MSP included) is English | **Resolve in D1** — this plan defaults to English to match `Inventory`, `RoleAssignment`, etc. |
| DG7 | `Inventory.get()` docstring caps service growth at 5 methods, "no non-Device operations" | N/A | **Moot** — this plan never touches `Inventory`, so the ceiling is never approached |

---

# 6. Target Domain Model

## 6.1 `Device` (extends the current dataclass — same file, same identity)

```python
@dataclass
class Device:
    # ── existing fields, unchanged ──
    name: str
    host: str
    vendor: str
    username: str
    encrypted_password: str
    platform: str = "ios"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    site_id: Optional[int] = None
    site_name: Optional[str] = None
    device_group_id: Optional[int] = None
    device_group_name: Optional[str] = None

    # ── new: lazily-resolved collaborators, not persisted, not serialized, not
    # part of the constructor signature (init=False — nobody passes these in) ──
    _vlan_driver: "BaseVendorDriver | None" = field(default=None, repr=False, compare=False, init=False)
    _port_driver: "BasePortDriver | None" = field(default=None, repr=False, compare=False, init=False)
    _password: "str | None" = field(default=None, repr=False, compare=False, init=False)

    # ── driver/password resolution — internal, lazy, cached after first call ──
    def _get_vlan_driver(self) -> "BaseVendorDriver":
        if self._vlan_driver is None:
            from app.core.config import EXECUTION_MODE
            if EXECUTION_MODE == "mock":
                from app.services.vendors.mock import MockVlanDriver
                self._vlan_driver = MockVlanDriver()
            else:
                from app.services.vendors.dispatcher import get_driver
                self._vlan_driver = get_driver(self)      # uses self.vendor, self.platform
        return self._vlan_driver

    def _get_port_driver(self) -> "BasePortDriver":
        if self._port_driver is None:
            from app.core.config import EXECUTION_MODE
            if EXECUTION_MODE == "mock":
                from app.services.vendors.mock import MockPortDriver
                self._port_driver = MockPortDriver()
            else:
                from app.services.vendors.dispatcher import get_port_driver
                self._port_driver = get_port_driver(self)
        return self._port_driver

    def _get_password(self) -> str | None:
        if self._password is None:
            from app.core.config import EXECUTION_MODE
            if EXECUTION_MODE != "mock":
                from app.services import secret_service
                self._password = secret_service.decrypt_password(self.encrypted_password)
        return self._password

    # ── VLAN (signatures updated post-D7 — see D7 for the read/write rationale) ──
    def create_vlan(self, vlan: VLAN) -> dict: ...       # calls vlan.validate_name()
    def delete_vlan(self, vlan: VLAN) -> dict: ...        # no name needed, no validate_name()
    def update_vlan_description(self, vlan: VLAN) -> dict: ...  # calls vlan.validate_name()
    def list_vlans(self) -> list[VLAN]: ...
    def save_config(self) -> dict: ...

    # ── Port ──
    def configure_port(self, config: PortConfigRequest) -> PortConfigResult: ...
    def set_port_admin_state(self, interface: str, enabled: bool) -> dict: ...
    def set_port_access_vlan(self, interface: str, vlan_id: int) -> dict: ...
    def set_trunk_allowed_vlans(self, interface: str, vlan_ids: list[int], mode: str) -> dict: ...
    def update_port_description(self, interface: str, description: str) -> dict: ...
    def list_ports(self) -> list[PortInfo]: ...
```

**Design notes:**

- **No Repository, no factory, no external wiring step.** Any `Device` — constructed
  by `Inventory.get()`, by `Inventory.list()`, by a test, by `device_service._to_domain`
  directly — works immediately. There is exactly one way to get a working `Device`:
  build one (from wherever) and call a method on it.
- `init=False` on the three private fields: they never appear in the dataclass's
  generated `__init__`, so nothing about how `Device(...)` gets constructed changes.
  `repr=False, compare=False` keeps `__eq__`/`__repr__` exactly as today — a test doing
  `assert device == Device(name=..., host=..., ...)` is unaffected by these fields
  existing or not being resolved yet.
- Every public method is a **one-line delegate**, mirroring exactly what
  `vlan_service.create_vlan_on_device` does today — a *move*, not a *rewrite*:
  ```python
  def create_vlan(self, vlan_id: int, name: str) -> dict:
      return self._get_vlan_driver().create_vlan(vlan_id, name, self, self._get_password())
  ```
- **Mock mode** is resolved inside `_get_vlan_driver`/`_get_port_driver` themselves —
  same `if EXECUTION_MODE == "mock"` check that today lives once per function in
  `vlan_service.py`, just centralized to one place instead of five. `Device`'s public
  methods (`create_vlan`, etc.) never branch on `EXECUTION_MODE` — that knowledge stays
  entirely inside the two private resolvers.
- **Lazy + cached**: `vendors.dispatcher.get_driver(self)`/`secret_service.decrypt_password`
  run **at most once per `Device` instance**, on whichever method is called first.
  `Inventory.list()` building 500 rows for a table view never triggers any of this —
  nothing calls `.create_vlan()` on a list row, so the resolvers never fire.
- **No orchestration inside `Device`.** No retry, no lock, no rollback, no Celery. Those
  stay exactly where they are. `Device.create_vlan()` is what
  `vlan_service.create_vlan_on_device()` is today — the innermost call that
  `orchestration_runner.run_operation`'s `execute` callback invokes.
  `vlan_execution_service.py`'s `run_create_job`/`enqueue_create_jobs` don't disappear;
  they change one line each (§9).

## 6.2 What `Inventory` needs from this (nothing)

`Inventory.get()`, `.list()`, `.register()`, `.move()`, `.deregister()` are called
exactly as today. `run_create_job` (etc.) fetches a device the same way it always
would (`inventory.get(device_name)`) and just calls a method on what comes back
instead of passing it to a free function — `Inventory` itself needs zero code changes.

---

# 7. What does NOT change (confidence list, mirrors MSP §20.3)

- `orchestration_runner.py` — zero changes. `run_operation`'s `execute` callback now
  points at `device.create_vlan(...)` instead of `vlan_service.create_vlan_on_device(...)`
  — same call shape, different owner.
- `vendors/dispatcher.py`, `vendors/{huawei,cisco}/*.py` — zero changes. Same driver
  classes, same playbooks, same `BaseVendorDriver`/`BasePortDriver` contracts.
- Celery task wrappers (`_group_create_task`, etc. in `vlan_execution_service.py`) —
  zero changes to their bodies. They still call `run_group_create_job`, which still
  calls `run_operation`.
- `Job`, `GroupJob`, `audit_service.py`, `device_locks.py`, `rate_limiter.py` — zero
  changes.
- MSP's `role_assignments`/`effective_role`/`core/scope.py`/`RoleAssignmentService` —
  zero changes. Device methods never check authorization; that responsibility stays
  entirely at the API router layer, exactly as it is today (`_authz_devices` in
  `vlans.py`/`ports.py`, which runs *before* anything touches `Device`).
  **Confirmed (2026-08-23)**: `RoleAssignmentService` was evaluated as a candidate to
  merge into `Inventory` (during the broader `services/` → classes review, see
  `plan-migracion-modelo-dominio.md` §11) and explicitly rejected — access control
  (who can do what) and device topology (`Inventory`'s job) are different bounded
  contexts; `Inventory.list()` already *consumes* role-assignment data as a client,
  it doesn't own it. This reinforces, not changes, the boundary this section already
  drew: `Device` has zero business being anywhere near authorization.

---

# 8. Diagram vs. this plan — discrepancies

Same spirit as MSP §8's audit of the diagram against real code:

| # | Discrepancy | Classification |
|---|---|---|
| E-A | Diagram method names are Spanish (`crear_vlan`, `listar_vlans`) | **Superseded by DG6/D0** — this plan uses English to match 100% of real code |
| E-B | Diagram shows `Inventory` as a class this plan would create, and implies `Device` gets its drivers from it | **Corrected during design review** — `Inventory` already existed (MSP Phase 3) *and* this plan doesn't touch it at all. There's no Repository-pattern precedent in this codebase to justify `Inventory` wiring `Device`'s collaborators (verified, §1) — `Device` resolves its own, lazily. |
| E-C | Diagram doesn't show `Device` composing a password field | **Implementation detail**, not a modeling gap — `_password` is decrypted-in-memory, never serialized |
| E-D | Diagram's `Orquestador` is drawn as a class `Device` calls directly | **Confirmed accurate at the concept level** — in code, the actual call path is `Device.create_vlan()` ← `run_operation`'s `execute` callback (an indirection the diagram simplifies away, same as MSP's D-C finding about `DeviceGroup ◇→ Device`) |

**Conclusion:** the diagram's *shape* for `Device`/drivers holds up; the `Inventory`
composition arrow needs to be redrawn as *not existing* — `Device` is self-sufficient,
`Inventory` only fetches/lists/moves/registers, it never wires anything onto what it
returns. Regenerate the diagram after Phase 2 (§14) lands, same discipline MSP applied.

---

# 9. Backend Changes

## 9.1 Files to modify

**`backend/app/models/device.py`**
- Add `_vlan_driver`, `_port_driver`, `_password` fields, all `init=False` (§6.1).
- Add `_get_vlan_driver()`, `_get_port_driver()`, `_get_password()` private resolvers.
- Add the 11 public methods (§6.1) as one-line delegates.

**`backend/app/services/vendors/mock.py`** (new file, per D1)
- `MockVlanDriver`, `MockPortDriver` — thin classes wrapping today's
  `_mock_create_vlan`/`_mock_delete_vlan`/etc. (currently module-level functions in
  `vlan_service.py`) so `Device`'s mock-mode resolvers (§6.1) can treat "mock" as a
  fourth vendor, symmetric with `vendors/dispatcher.py`'s real-vendor branches.

**`backend/app/services/vlan_execution_service.py`**
- `run_create_job`, `run_delete_job`, `run_update_job`, `run_save_job`: change the
  `execute=lambda: vlan_service.create_vlan_on_device(...)` line to
  `execute=lambda: inventory.get(device).create_vlan(vlan_id, name)` (one line each,
  ×5 call sites — `inventory.get(...)` is called exactly as today, nothing about
  `Inventory` changes). Everything else in these functions — pre-state capture,
  rollback callbacks, Celery task wrappers — **unchanged**.

**`backend/app/services/port_execution_service.py`, `port_config_service.py`**
- Same one-line-per-call-site change, mirrored for Port operations.

## 9.2 Files to deprecate (not delete yet — §14 phases it)

- `backend/app/services/vlan_service.py` — once every call site in
  `vlan_execution_service.py` routes through `Device`, this file's 5 functions have
  no callers left. Confirm with a grep before deleting (mirrors MSP's R2 discipline).
- Port equivalent logic inside `port_execution_service.py`/`port_config_service.py` —
  same treatment; these files don't disappear (they still own retry/rollback wiring),
  only their internal driver-resolution logic does.

## 9.3 Files that are unaffected

`orchestration_runner.py`, `vendors/dispatcher.py`, `vendors/{huawei,cisco}/*.py`,
`worker.py`, `job_service.py`, `group_job_service.py`, `audit_service.py`,
`device_locks.py`, `rate_limiter.py`, `retry_policy.py`, `ansible/**`,
**`inventory_service.py` in full — zero changes to any of its 5 methods**,
**`device_service.py` in full**, `core/scope.py`, `core/effective_role.py` (MSP's
authz layer — Device never touches it).

**On `device_service.py` specifically** — this is a distinct module from
`vlan_service.py` and this plan does **not** move its functions into `Device`, ever.
The boundary: `device_service.py` (`create_device`, `get_device`, `update_device`,
`delete_device`, `seed_defaults`, `clear_devices`, plus its private mapper/validation
helpers) manages a device's **identity and persistence** — the DB row, which Site/Group
it belongs to. That's the territory MSP's `Inventory` explicitly claims ownership of
("sole entry point for Device CRUD, listing, and movement") and it's a closed layer
this plan doesn't reopen (§12). `Device`'s new methods (§6) are a completely different
kind of operation — *using* an already-identified device to talk to the network
equipment (VLAN, Port). None of `get_device`/`create_device`/etc. have a sensible
reading as "a thing an existing `Device` instance does to itself" — `get_devices()` is
a collection query, `create_device(...)` builds a brand-new row before any `Device`
object exists, `seed_defaults()`/`clear_devices()` are bootstrap/test utilities. If any
of them ever did move, they'd move into `Inventory` (MSP's territory), not `Device`
(this plan's territory) — but that's not this plan's call to make.

---

# 10. API Changes

**None required at the endpoint contract level.** `POST /api/v1/vlans/`,
`PATCH /api/v1/ports/*`, etc. keep their exact request/response shapes — this plan is
entirely internal to the service layer. The only visible change is **where** the code
that used to live in `vlan_service.py`/`port_service.py`-equivalent files now lives
(inside `Device`), which is invisible to any API client.

---

# 11. Testing Strategy

## 11.1 New tests (constructor injection — no monkeypatch)
- `test_device_create_vlan_delegates_to_driver.py` — construct a `Device`, reach in
  and set `device._vlan_driver = Fake()` directly (no need to fake `Inventory` at all
  — `Device` has no dependency on it), call `.create_vlan(...)`, assert
  `Fake.create_vlan` was called with the right args.
- `test_device_mock_mode_uses_mock_driver.py` — under `EXECUTION_MODE=mock`, a plain
  `Device(...)` (constructed directly, no `Inventory` involved) resolves
  `MockVlanDriver` on first call to `.create_vlan()`.
- `test_device_resolves_driver_once.py` — call `.create_vlan()` then `.list_vlans()`
  on the same `Device` instance; assert `vendors.dispatcher.get_driver` was invoked
  exactly once (not per method call) — this is the actual perf/correctness claim of
  the whole plan (DG2), worth its own regression test.
- `test_device_list_never_resolves_drivers.py` — build 500 `Device` objects (as
  `Inventory.list()` would), touch only dataclass fields, assert
  `vendors.dispatcher.get_driver`/`secret_service.decrypt_password` were **never**
  called — proves the laziness actually avoids the cost list views don't need.

## 11.2 Existing tests — must keep passing unmodified
- `test_vlan_semantics.py`, `test_rollback_hardened.py`, `test_port_*.py` — these
  exercise `run_create_job`/`enqueue_create_jobs` end-to-end via `TestClient`. Since
  §9.1's changes are one-line internal swaps with identical driver call signatures,
  these should pass with **zero test-file edits**. If any fail, that's a signal the
  swap wasn't behavior-preserving — treat as a blocking bug, not an expected test update.
- The `monkeypatch.setattr("app.api.ports._capture_pre_state_...")` seams (~30 call
  sites, per earlier analysis) are untouched by this plan — they patch pre-state
  capture, which stays in `*_execution_service.py`, not in `Device`.

## 11.3 Snapshot-diff test (mirrors MSP's R3 discipline)
- `test_device_vlan_output_matches_legacy.py` — for a fixed set of
  (vlan_id, name, device) triples under mock mode, assert
  `vlan_service.create_vlan_on_device(...)` and
  `inventory.get(device).create_vlan(vlan_id, name)` return byte-identical dicts.
  Run this **before** deleting `vlan_service.py` (§9.2).

---

# 12. Non-Goals

Explicitly out of scope for this plan (do not bundle):
- `InterfazVirtual` / `ConfiguracionGlobal` (RF-INTERV, RF-GLOBAL) — separate work,
  `plan-migracion-modelo-dominio.md` §3-4. Can start once this plan's Phase 1 lands,
  reusing the same `Device` composition pattern for 2 more driver pairs.
- Generic playbooks + `PlantillaComando` in DB (`plan-migracion-modelo-dominio.md` §5-6)
  — orthogonal; can land before, after, or interleaved with this plan.
- Anything in `role_assignments`/`effective_role`/MSP's Site-Group hierarchy — not
  touched, not needed, not blocking.
- `device_service.py`'s CRUD functions (`create_device`, `get_device`, `update_device`,
  `delete_device`, `seed_defaults`, `clear_devices`) — identity/persistence management
  is `Inventory`'s explicitly claimed territory (§4). None of it moves into `Device`.
- `DeviceGroup` rich methods (`crear_vlan_en_grupo` in the old plan) — depends on this
  plan landing first (needs `Device` methods to exist before a group can fan out to
  them), tracked separately.

---

# 13. Open Questions / Decisions Required

## D0 — RESOLVED. English method names.
`create_vlan`, `list_ports`, etc. — matches 100% of real code, including everything
MSP built (`Inventory`, `RoleAssignment`, `effective_role`). The diagram gets
regenerated in English at Phase 3 (§14).

## D1 — RESOLVED. D1b — `services/vendors/mock.py`.
`MockVlanDriver`/`MockPortDriver` live in their own file, alongside `huawei/`, `cisco/`
— "mock" becomes a fourth vendor branch, symmetric with `vendors/dispatcher.py`, instead
of infrastructure code embedded in `models/device.py`.

## D2 — RESOLVED. `Device` resolves its own drivers; `Inventory` is untouched.
Originally scoped as "extend `Inventory.get()` to attach drivers" (which raised a real
question about MSP's documented 5-method ceiling on `Inventory`). Superseded during
design review: **verified there is no Repository-pattern precedent anywhere in this
codebase** (`git grep -i repository` → zero hits; `device_service.py`/
`inventory_service.py` both touch SQLAlchemy directly, no abstraction layer exists
whose stated job is "construct a fully-wired domain object"). Without that precedent,
there was no real justification for centralizing driver-wiring in `Inventory` — and
doing so would have made every `Device` built from anywhere *other* than
`Inventory.get()` (e.g. `Inventory.list()`'s internal rows, or a future test helper)
silently non-functional until someone remembered to wire it. Lazy self-resolution
(§6.1) fixes both problems at once: `Inventory` needs zero changes, and *every*
`Device`, from *any* origin, works the moment you call a method on it.

## D2-addendum — REOPENED then RESOLVED (2026-08-23). `Device` still called free
functions, not classes.
**Issue found after Phase 1 shipped.** `_get_vlan_driver()`/`_get_port_driver()` call
`vendors.dispatcher.get_driver(self)`/`get_port_driver(self)` — module-level
functions. `_get_password()` calls `secret_service.decrypt_password(...)` — same.
D2's core claim ("`Device` resolves its own collaborators, `Inventory` stays
untouched") is still correct — but *what* it resolves them through was still
free-function `services/` code, which contradicts the broader goal stated for this
codebase: every unit of behavior with real responsibility (state to hold, or a
cohesive set of operations on one concept) becomes a class, not a module of
functions (see `plan-migracion-modelo-dominio.md` §11 for the full policy and the
criterion for when a function legitimately stays a function).
**Resolution**: see D5 and D6 below. `Device`'s three private resolvers change to
call class methods instead of module functions. This is a **follow-up phase**
(§14 Phase 1b), not a revert of Phase 1 — the lazy/cached design and the 11 public
methods (§6.1) do not change shape, only what their private resolvers call.

## D5 — REVISED (2026-08-23). `vendors/dispatcher.py` becomes `VendorDriverFactory` —
**only if redesigned as a mutable registry; deprioritized otherwise.**
**Original resolution (superseded below).** Today `get_driver(device)`/
`get_vendor_driver(vendor, platform)`/`get_port_driver(device)`/
`get_port_vendor_driver(vendor, platform)` are 4 module-level functions with no
state — just an `if vendor in _CISCO_VENDORS: ...` routing table. The first pass at
D5 proposed wrapping this unchanged into a class with the same `if`/`elif` body
moved into a method.
**Why that's not good enough.** Audited against
`plan-migracion-modelo-dominio.md` §11's criterion (state to hold, or unifying
functions scattered across files — not "wrap a function for its own sake"):
`dispatcher.py`'s 4 functions already live in **one file**, and the `frozenset`
routing tables don't change at runtime — there is no real state to encapsulate. A
thin wrapper class here would be the same category of ungrounded justification
already caught once this session (the "Repository" argument in D2). This is now
the **weakest-justified** of the classes proposed for `Device`'s own dependencies
(see `plan-migracion-modelo-dominio.md` §11, "Débil o incierta").
**Resolution.** Only implement `VendorDriverFactory` if it's redesigned as a
genuine mutable registry — real state, real Open/Closed win:
```python
class VendorDriverFactory:
    def __init__(self):
        self._vlan_drivers: dict[str, type[BaseVendorDriver]] = {}
        self._port_drivers: dict[str, type[BasePortDriver]] = {}

    def register_vlan_driver(self, *vendors: str, driver_cls: type[BaseVendorDriver]) -> None:
        for v in vendors:
            self._vlan_drivers[v] = driver_cls

    def get_vlan_driver(self, vendor: str, platform: str) -> BaseVendorDriver:
        driver_cls = self._vlan_drivers.get(vendor)
        if driver_cls is None:
            raise ValueError(f"No driver registered for vendor='{vendor}' platform='{platform}'")
        return driver_cls()
    # mirror for port drivers
```
A module-level singleton instance (e.g. `factory = VendorDriverFactory()`,
populated at import time with the existing Cisco/Huawei registrations) replaces
`dispatcher.py`'s functions; `Device._get_vlan_driver()` calls
`factory.get_vlan_driver(self.vendor, self.platform)`. New vendors register instead
of requiring an `if`/`elif` edit — the actual justification, not just consistency
with `Device`. **Deprioritized in Phase 1b** (see §14) until this redesign is
confirmed worth doing; `Device` calling `dispatcher.get_driver(self)` as a free
function is not incorrect, just inconsistent — lower urgency than D6/D7.

## D6 — RESOLVED (2026-08-23). `secret_service.py` becomes `SecretVault`.
Today `decrypt_password(encrypted)` is a module function wrapping a module-level
`_fernet` built from `FERNET_KEY`. Becomes a class (`SecretVault`) holding the
`Fernet` instance as real instance state instead of a module global, exposing
`.decrypt(encrypted) -> str`. `Device._get_password()` calls
`secret_vault.decrypt(self.encrypted_password)` instead of
`secret_service.decrypt_password(...)`. Encapsulating the `Fernet` instance as
object state (rather than a module-level singleton) is the concrete win here, not
just cosmetics — it's the same category of change as D5.
**Shipped (2026-08-23)**: `SecretVault` class added to `secret_service.py`;
`encrypt_password`/`decrypt_password` module functions kept as thin delegates to a
module-level `vault = SecretVault()` singleton (17 other callers across
`vlan_service.py`, `port_service.py`, `device_service.py`, `inventory_service.py`
untouched, confirmed via grep before deciding — replacing them outright was out of
Phase 1b's scope). `Device._get_password()` calls
`from app.services.secret_service import vault; vault.decrypt(...)` directly.

## D7 — RESOLVED and IMPLEMENTED (2026-08-23). `VLANInfo` renamed to `VLAN` — one
class, not a `VLANInfo`/`VLANRequest` pair.
**Issue.** `Device.create_vlan(vlan_id, name)` took raw primitives — inconsistent
with `Device.configure_port(config: PortConfigRequest)`, which takes a
self-validating object. VLAN had no equivalent; `vlan_id` range/reserved/name-format
validation lived only in `api/vlans.py` (`validators/vlan_validator.py`'s 4
functions: `validate_vlan_id_range`, `validate_vlan_not_reserved`,
`validate_vlan_name`, `validate_description`), disconnected from any VLAN object.
**User explicitly rejected a two-class split** ("no quiero que se cree otro objeto
... tiene que haber una sola clase Vlan") — `VLANInfo` (13-file, 83-occurrence
rename) became `VLAN`, serving both the read side (what parsers build from a real
device) and the write side (what `Device` accepts).
**Read/write asymmetry found and resolved during implementation.** Validating all 4
rules unconditionally at construction would have broken the read path:
`parsers/vlan_parser.py`'s `_parse_vrp_tabular` explicitly joins multi-word
descriptions (`" ".join(parts[9:])`) — real devices report names with spaces
(confirmed with a live repro: `"Guest Network"`), which `validate_vlan_name`'s regex
(`^[A-Za-z0-9._-]+$`) rejects. Resolution (confirmed via AskUserQuestion):
- `VLAN.__post_init__` validates `vlan_id` **unconditionally** (range + not-reserved)
  — safe for both paths, since parsers already exclude reserved VLANs (`_VRP_INTERNAL`/
  `_IOS_INTERNAL`) before constructing one.
- `name` format validates **only** via an explicit `vlan.validate_name()` method —
  not run automatically. `Device.create_vlan`/`update_vlan_description` call it
  before delegating to the driver; `Device.delete_vlan` never calls it (doesn't need
  a name). `name` defaults to `""` so `VLAN(vlan_id=X)` is valid for delete.
**This does not reverse D4** — `Device` still implements no validation *logic*
itself; it invokes the object's own `validate_name()` at the right moment, the same
way `configure_port` relies on `PortConfigRequest.__post_init__` having already run.
`api/vlans.py`'s existing `vlan_validator` calls are unaffected (Phase 1b doesn't
touch `api/vlans.py` — that's Phase 2 territory); the two will be reconciled when
Phase 2 wires `Device` into the API layer.
**Shipped**: `models/vlan.py` (`VLAN` class + `validate_name()`), `models/device.py`
(`create_vlan`/`delete_vlan`/`update_vlan_description` take `VLAN`), 9 new tests in
`tests/test_vlan_domain_object.py` covering both the validation asymmetry and the
Device wiring. Full suite green: 972 passed, 7 skipped.

## D3 — REVISED (2026-08-23). `vlan_service.py` deletion is no longer the goal —
superseded by how Phase 2 actually shipped.
**Original text (superseded, kept for history):** delete `vlan_service.py` once
migrated and confirmed unused, mirroring MSP's M4 "point of no return" discipline —
Phase 3, after a snapshot-diff test compares old-vs-new output.
**Why this changed.** That design assumed `vlan_service.py` would keep its own
duplicated resolve/decrypt/dispatch logic while callers moved to `Device` directly,
leaving two implementations to diff and then one to delete. Phase 2 shipped
differently (see Phase 2 status, §14): `vlan_service.py`'s functions themselves
became the one-line `Device` delegates. There is no second implementation left to
diff against, and no duplicated logic left to justify deleting the file — it's a
5-function compatibility facade that every existing caller (`vlan_execution_service
.py`, `api/vlans.py`, dozens of tests) still legitimately calls by name.
**Current recommendation**: leave `vlan_service.py` in place indefinitely as a thin
facade. Revisit deletion only if someone later wants to also inline
`vlan_execution_service.py`'s 11 call sites directly to `Inventory().get(device)
.method(...)` *and* migrate the tests that monkeypatch `vlan_service.*` by name — a
strictly optional, separate, larger piece of work with no remaining architectural
benefit (the "stop duplicating resolve/decrypt/dispatch" goal is already achieved).

## D4 — Should `Device` validate inputs (vlan_id range, etc.) or stay a thin delegate?
**Issue.** Today, `vlan_validator.validate_vlan_id_range` runs in `api/vlans.py`,
*before* `vlan_execution_service` is ever called. Should `Device.create_vlan()` also
validate, or trust the caller?
**Recommendation.** Trust the caller (no double validation). `Device` is an internal
domain object called only from already-validated code paths (`run_create_job`); adding
validation here would duplicate `api/vlans.py`'s checks for no safety benefit, and
diverge if the two validation rules ever drift.
**Still stands after D7** — D7 (below) gives VLAN a self-validating request object,
but the validation lives on that object's `__post_init__`, not inside `Device`.
`Device.create_vlan` remains a thin delegate; see D7 for what changes.

---

# 14. Phased Task Breakdown

## Phase 0 — Prerequisites
- Resolve D0, D1, D3, D4 (D2 already resolved).
- Grep-audit: every current caller of `vlan_service.create_vlan_on_device` (and its 4
  siblings) and the Port equivalents — confirm the call-site list matches §9.1
  exactly (no forgotten caller, e.g. a test helper or a script under `scripts/`).

## Phase 1 — Additive: `Device` gains methods, nothing switches over yet
- T1.1 — Add the 3 private fields (`init=False`) + 3 private resolvers + 11 public
  methods to `models/device.py` (§6.1).
- T1.2 — New file `services/vendors/mock.py` per D1, with `MockVlanDriver`/
  `MockPortDriver`. `vlan_service.py`/Port-equivalent logic **still exists and is
  still called by the old code paths** — nothing is wired to the new `Device` methods
  yet. `inventory_service.py` is **not touched** in this phase or any other.
- T1.3 — New tests per §11.1 (constructor injection, no `Inventory` involvement at
  all). These exercise `Device` in isolation; they do not yet prove the swap is safe
  end-to-end.
- **Gate:** `pytest backend/tests/` green (nothing changed for existing call sites).
- **Status: DONE (2026-08-23)** — 963 passed, 7 skipped, 0 modified. See D2-addendum
  below for the gap found right after this phase shipped.

## Phase 1b — Close the free-function gap found in Phase 1 (D6, D7; D5 optional/deferred)
Not part of the original plan — added after Phase 1 shipped and a review surfaced
that `Device`'s own private resolvers still called free functions in `services/`
(D2-addendum). Still additive: nothing outside `Device`'s three private resolvers
and the small classes below changes. **Reprioritized (2026-08-23)** after auditing
each candidate class's real justification (`plan-migracion-modelo-dominio.md` §11):
D5 (`VendorDriverFactory`) is the weakest-justified of the two and is **deferred**
until/unless it's redesigned as a mutable registry (see revised D5) — implementing
it as a thin wrapper today would be the same ungrounded-class mistake already
caught once this session. D6 (`SecretVault`) has real justification (genuine
module-level state today) and proceeds.
- T1b.1 — `secret_service.py`: add `SecretVault` class per D6. **DONE** — kept
  `decrypt_password`/`encrypt_password` as thin delegates to a `vault` singleton
  (grep confirmed 17 other callers, replacing them outright was out of scope).
- T1b.2 — `models/vlan.py`: rename `VLANInfo` → `VLAN` per D7 (13 files, 83
  occurrences, mechanical rename), add `__post_init__` (vlan_id validation,
  unconditional) and `validate_name()` (name validation, explicit call only).
  **DONE**.
- T1b.3 — `models/device.py`: `_get_password` calls `SecretVault`'s `vault`
  singleton; `create_vlan`/`delete_vlan`/`update_vlan_description` accept `VLAN`.
  `_get_vlan_driver`/`_get_port_driver` keep calling `dispatcher.get_driver`/
  `get_port_driver` (free functions) for now — **not** a regression, just D5
  staying deferred; revisit once D5's registry redesign is confirmed worth doing.
  Updated the Phase 1 tests (§11.1) that constructed raw `(vlan_id, name)` args to
  build a `VLAN` instead; added `tests/test_vlan_domain_object.py` (9 tests) for
  the read/write validation asymmetry. **DONE**.
- **Gate:** `pytest backend/tests/` green. **Status: DONE (2026-08-23)** — 972
  passed, 7 skipped, 0 modified beyond the mechanical `VLANInfo`→`VLAN` rename and
  the Phase-1-test signature updates.
- T1b.4 (optional, only if D5's registry redesign is confirmed — **not started**) —
  `vendors/dispatcher.py`: add `VendorDriverFactory` as a mutable registry (not a
  thin wrapper — see revised D5), populate it at import time with the existing
  Cisco/Huawei registrations, wire `Device._get_vlan_driver`/`_get_port_driver` to
  call it instead of the free functions.

## Phase 2 — VLAN cutover: DONE (2026-08-23), via a different mechanism than planned
**What actually happened, and why it's better than the original T2.1 design.**
The original plan had `vlan_execution_service.py`'s 11 call sites (not just 4 — a
grep-audit done at implementation time found pre-state capture, post-verify, and 3
rollback functions also call `vlan_service.*`, undercounted in the original design)
swap directly to `inventory.get(device).method(...)`. Implementing that literally
broke ~47-53 tests: dozens of tests monkeypatch `vlan_service.create_vlan_on_device`/
`get_vlans`/etc. **by module attribute name** to simulate real-mode execution —
routing `vlan_execution_service.py` around `vlan_service.py` entirely made those
patches land on a function nothing calls anymore.

**Resolution actually implemented**: `vlan_execution_service.py` is **unchanged**
(reverted after the first attempt). Instead, `vlan_service.py`'s 5 public functions
(`create_vlan_on_device`, `delete_vlan`, `update_vlan_description`, `get_vlans`,
`save_config_on_device`) became one-line delegates to `Inventory().get(device_id)
.method(...)` — this is DG3's original goal ("Collapsed into 5 one-line Device
methods") achieved *inside* `vlan_service.py` rather than by making its callers
bypass it. Every existing monkeypatch on `vlan_service.*` by name keeps working
unchanged, because `monkeypatch.setattr(module, "attr", fake)` + the caller doing
`module.attr(...)` (attribute lookup at call time) doesn't care what the real
implementation does. Only 6 tests (not ~47) needed a 1-line addition — they patched
`vlan_service.EXECUTION_MODE = "real"` specifically to route into the real Huawei/
Cisco→`ansible_service.run_playbook` chain without flipping the global mode; since
`Device._get_vlan_driver()` checks the global `app.core.config.EXECUTION_MODE`, not
`vlan_service`'s local copy, those 6 needed `monkeypatch.setattr("app.core.config
.EXECUTION_MODE", "real")` added alongside their existing patch
(`test_smart_retry.py`×4, `test_observability.py`×1, `test_multi_device_vlan.py`×1).
- **Gate:** full `pytest backend/tests/` green. **Status: DONE** — 972 passed, 7
  skipped, 0 failures.
- **Consequence for D3** (below): re-evaluate — `vlan_service.py` is no longer 5
  near-identical resolve/decrypt/dispatch functions (the thing DG3 objected to); it's
  now a 5-line compatibility facade with zero duplicated logic. Deleting it is no
  longer required to achieve the goal — see D3.

## Phase 2 — Port: not started
Same swap, mirrored for `port_service.py`'s equivalent (`port_execution_service.py`/
`port_config_service.py` currently have driver-resolution logic inline, no single
`port_service.py`-shaped file to collapse into — needs its own investigation before
assuming the VLAN approach transfers directly).
- T2.3 — Investigate `port_execution_service.py`/`port_config_service.py`'s actual
  call graph (grep-audit, same discipline as VLAN) before designing the swap.
- T2.4 — Snapshot-diff test (§11.3) for Port — green before proceeding, if still
  applicable given the VLAN experience above.

## Phase 3 — Cleanup
- T3.1 — Grep confirms zero remaining callers of `vlan_service.py`'s 5 functions and
  the Port-equivalent driver-resolution code.
- T3.2 — Delete `vlan_service.py`. Fold any remaining non-driver logic (mock-mode
  bookkeeping, if any survives) into `Device`'s mock resolvers.
- T3.3 — Regenerate `diagrama_clases_dominio.drawio`'s `Device`/`Inventory` region per
  §8 (English method names; remove the `Inventory → Device` "compone" arrow entirely
  — `Device` composes its drivers itself, `Inventory` composes nothing).

## Phase 4 (optional, tracked separately) — `InterfazVirtual` / `ConfiguracionGlobal`
- Not started here. Once Phase 3 lands, `plan-migracion-modelo-dominio.md` §3-4's
  remaining work (2 new driver pairs) follows the exact same composition pattern this
  plan establishes for VLAN/Port — no new architecture to invent.

---

*End of Device Implementation Plan.*

*Nothing in this document should be implemented until Section 13 decisions are confirmed.*
