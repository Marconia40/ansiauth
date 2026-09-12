"""End-to-end audit trail tests.

Ported from the pre-migration audit_service/user_service-based suite to the
current Repository[T]/Orquestador architecture. Live-verified against the
`client`/`operator_client` fixtures before porting the AUD-001 "append-only
chain" tests below (as instructed) -- findings:

* Orquestador.ejecutar() dispatches exactly ONE terminal DomainEvent
  ("recurso_aplicado" or "recurso_fallido") per job, at the very end. There
  is no "pending" event fired when the job starts, and GroupOperationRunner.
  encolar() (the synchronous part of POST /vlans) does not write any audit
  row itself either. So a VLAN create now produces exactly ONE audit row,
  never two -- the old pending-row + parent_audit_id-linked follow-up row
  chain has no equivalent anymore. The 3 tests that asserted that chain
  (test_status_progression_creates_separate_rows,
  test_follow_up_row_links_to_parent,
  test_follow_up_row_inherits_fields_from_parent) are dropped below with a
  comment, per the task's "AuditListener may write differently" guidance.
* AuditRecord.desde() (the factory AuditListener uses for every
  Orquestador-driven event) sets action from `payload["accion"]`, which for
  VLAN is Spanish: "crear_vlan"/"eliminar_vlan"/"actualizar_vlan" -- not the
  old "create_vlan"/"delete_vlan"/"update_vlan". status is "success"/
  "failure" (not "completed"/"failed"/"pending"). `details` is the raw
  vendor execution result (rc/stdout/stderr/accion) -- it no longer carries
  vlan_id/name/description, so entries are now identified by their
  `summary` field (VLAN.resumen_intento(): "VLAN <id>: set name to
  '<name>'" for create/update, "Delete VLAN <id>" for delete) instead of
  `details["vlan_id"]`. `job_id`/`request_id` are always None on these rows
  -- AuditRecord.desde() never populates them (a real gap vs. the old
  behavior; flagged in the PR description, not fixed here per scope).
* GET /api/v1/audit/ now returns the standard `ok()` envelope with the
  records nested at `data.items` (plus `data.total`/`page`/`page_size`),
  not a bare list.
"""
import time
from datetime import datetime, timezone, timedelta

import pytest

from app.composition import (
    audit_repository,
    device_repository,
    plugin_registry,
    role_assignment_repository,
    site_repository,
    user_repository,
)
from app.db.models import SiteModel
from app.db.session import get_session
from app.models.audit import AuditRecord
from app.repositories.role_assignment_repository import RoleAssignment
from app.services.vendors.mock import MockVendor

_TEST_SITE_NAME = "AuditTestSite"


def _clear_audit_log() -> None:
    from app.db.models import AuditLogModel
    with get_session() as session:
        session.query(AuditLogModel).delete(synchronize_session=False)


def _audit_items(client, **params) -> list[dict]:
    resp = client.get("/api/v1/audit/", params=params)
    return resp.json()["data"]["items"]


def _entries_for_vlan(log: list[dict], vlan_id: int, action: str | None = None) -> list[dict]:
    """VLAN audit rows no longer carry vlan_id in `details` -- match on the
    `summary` field instead (see module docstring)."""
    needle = f"VLAN {vlan_id}"
    out = []
    for e in log:
        if e["resource"] != "vlan":
            continue
        if action is not None and e["action"] != action:
            continue
        if needle in (e.get("summary") or ""):
            out.append(e)
    return out


@pytest.fixture(autouse=True)
def clear_log():
    _clear_audit_log()
    yield
    _clear_audit_log()


@pytest.fixture(autouse=True)
def _audit_test_infra(admin_client):
    """Registers mock_device/fail_device (used throughout this file) under
    a dedicated REGULAR site, and grants operator/observer roles on that
    site so the non-admin client fixtures can act on them.

    Devices didn't exist at all under the old architecture's mock mode
    (VLAN ops never checked for a registered Device row) -- POST /vlans now
    404s against an unregistered device name (Inventory-backed
    require_device()). Also forces the cisco_ios/huawei_vrp plugin slots to
    MockVendor: this repo's .env pins EXECUTION_MODE=real for local dev,
    which composition.py bakes into plugin_registry at import time (before
    any test fixture runs) -- without this override these tests would
    attempt real SSH against a fake host.
    """
    plugin_registry.registrar("cisco_ios", MockVendor())
    plugin_registry.registrar("huawei_vrp", MockVendor())

    with get_session() as session:
        row = session.query(SiteModel).filter_by(name=_TEST_SITE_NAME).first()
        site_id = row.id if row else None
    if site_id is None:
        site_id = site_repository.crear_con_grupo_default(_TEST_SITE_NAME).id

    # Grant on whichever site actually owns each device -- if another test
    # file's fixture already registered "mock_device"/"fail_device" under
    # ITS OWN site (device names are global; these fixtures run across
    # many files sharing one DB), granting only on `site_id` here would
    # leave operator/observer unable to see a device that landed elsewhere.
    site_ids_to_grant = set()
    for name in ("mock_device", "fail_device"):
        existing = device_repository.get(name)
        if existing is None:
            resp = admin_client.post("/api/v1/devices/", json={
                "name": name, "host": "10.0.0.1", "vendor": "cisco_ios",
                "username": "admin", "password": "admin", "site_id": site_id,
            })
            assert resp.status_code == 200, resp.text
            site_ids_to_grant.add(site_id)
        else:
            site_ids_to_grant.add(existing.site_id)

    for username in ("operator", "observer"):
        user = user_repository.obtener_por_username(username)
        if user is None:
            continue
        scope = role_assignment_repository.scope_de({"id": user.id, "is_system_admin": False})
        for sid in site_ids_to_grant:
            if scope.rol_para(sid) != username:
                role_assignment_repository.add(RoleAssignment(user_id=user.id, site_id=sid, role=username))
    yield


# --- Access control ---

def test_audit_log_scoped_for_non_system_admin(observer_client, operator_client):
    """MSP: Phase 4 (D27) — the audit endpoint no longer gates on the admin
    role. Non system-admin callers get 200 with a *scoped* result set:
    only rows for devices/groups/sites their grants cover, plus
    device-unrelated auth events. See test_msp_audit_scoping.py for
    positive-side coverage."""
    r_obs = observer_client.get("/api/v1/audit/")
    r_op = operator_client.get("/api/v1/audit/")
    assert r_obs.status_code == 200, r_obs.text
    assert r_op.status_code == 200, r_op.text
    assert isinstance(r_obs.json()["data"]["items"], list)
    assert isinstance(r_op.json()["data"]["items"], list)


def test_audit_log_without_token(unauth_client):
    assert unauth_client.get("/api/v1/audit/").status_code == 401


def test_audit_log_accessible_by_admin(admin_client):
    response = admin_client.get("/api/v1/audit/")
    assert response.status_code == 200
    assert isinstance(response.json()["data"]["items"], list)


# --- VLAN auditing ---

def test_create_vlan_is_audited(operator_client, admin_client):
    resp = operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})
    assert resp.status_code == 202, resp.text

    log = _audit_items(admin_client)
    entries = _entries_for_vlan(log, 50, action="crear_vlan")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["resource"] == "vlan"
    assert entry["user"] == "operator"
    assert entry["device"] == "mock_device"
    # rc=0 stdout/accion from the mock driver -- vlan_id/name live in
    # `summary` now, not `details` (see module docstring).
    assert entry["details"]["accion"] == "crear_vlan"
    assert entry["status"] == "success"


def test_create_vlan_multi_device_audit_rows(operator_client, admin_client):
    operator_client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 51, "name": "MULTI", "devices": ["mock_device", "fail_device"]},
    )

    log = _audit_items(admin_client)
    entries = _entries_for_vlan(log, 51)
    # 1 row per device now (no pending+follow-up chain) -- one succeeds
    # (mock_device), one fails (fail_device).
    assert len(entries) == 2
    by_device = {e["device"]: e for e in entries}
    assert by_device["mock_device"]["status"] == "success"
    assert by_device["fail_device"]["status"] == "failure"


def test_audit_status_updates_after_execution(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 52, "name": "STATUSTEST", "devices": ["mock_device"]})

    log = _audit_items(admin_client)
    entries = _entries_for_vlan(log, 52)
    assert len(entries) == 1
    assert entries[0]["status"] == "success"


def test_audit_status_failed_on_device_error(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 53, "name": "FAILAUDIT", "devices": ["fail_device"]})

    log = _audit_items(admin_client)
    entries = _entries_for_vlan(log, 53)
    assert len(entries) == 1
    assert entries[0]["status"] == "failure"
    assert entries[0]["device"] == "fail_device"


def test_delete_vlan_is_audited(admin_client):
    admin_client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})

    log = _audit_items(admin_client)
    entries = _entries_for_vlan(log, 10, action="eliminar_vlan")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["resource"] == "vlan"
    assert entry["user"] == "admin"
    assert entry["status"] == "success"


def test_update_vlan_is_audited(operator_client, admin_client):
    operator_client.patch("/api/v1/vlans/10", json={"description": "Core-VLAN", "devices": ["mock_device"]})

    log = _audit_items(admin_client)
    entries = _entries_for_vlan(log, 10, action="actualizar_vlan")
    assert len(entries) == 1
    entry = entries[0]
    assert entry["resource"] == "vlan"
    assert entry["user"] == "operator"
    assert entry["status"] == "success"


# --- Jobs auditing ---

def test_cancel_job_is_audited(admin_client):
    from app.composition import job_repository
    from app.models.job import Job

    job = Job()
    job_repository.add(job)

    admin_client.post(f"/api/v1/jobs/{job.job_id}/cancel")

    log = _audit_items(admin_client)
    entry = next(e for e in log if e["action"] == "cancel_job")
    assert entry["resource"] == "job"
    assert entry["user"] == "admin"
    assert entry["details"]["job_id"] == job.job_id


# --- Auth auditing ---

def test_login_success_is_audited(unauth_client, admin_client):
    unauth_client.post("/api/v1/auth/login", data={"username": "operator", "password": "operator123"})

    log = _audit_items(admin_client)
    entry = next(e for e in log if e["action"] == "login" and e["status"] == "success")
    assert entry["resource"] == "auth"
    assert entry["user"] == "operator"


def test_login_failure_is_audited(unauth_client, admin_client):
    unauth_client.post("/api/v1/auth/login", data={"username": "operator", "password": "wrongpass"})

    log = _audit_items(admin_client)
    entry = next(e for e in log if e["action"] == "login" and e["status"] == "failed")
    assert entry["resource"] == "auth"
    assert entry["user"] == "operator"


# --- Filters ---

def test_filter_by_user(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})
    admin_client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})

    log = _audit_items(admin_client, user="operator")
    assert all(e["user"] == "operator" for e in log)
    assert any(e["action"] == "crear_vlan" for e in log)


def test_filter_by_action(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})
    admin_client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})

    log = _audit_items(admin_client, action="crear_vlan")
    assert all(e["action"] == "crear_vlan" for e in log)
    assert len(log) >= 1


def test_filter_by_resource(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})
    admin_client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})
    operator_client.patch("/api/v1/vlans/10", json={"description": "x", "devices": ["mock_device"]})

    log = _audit_items(admin_client, resource="vlan")
    assert len(log) > 0
    assert all(e["resource"] == "vlan" for e in log)


# --- Pagination ---

def test_pagination_limit(operator_client, admin_client):
    for i in range(5):
        operator_client.post("/api/v1/vlans/", json={"vlan_id": 100 + i, "name": f"V{i}", "devices": ["mock_device"]})

    log_all = _audit_items(admin_client)
    log_limited = _audit_items(admin_client, limit=2)
    assert len(log_limited) == 2
    assert len(log_all) > 2


def test_pagination_skip(operator_client, admin_client):
    for i in range(4):
        operator_client.post("/api/v1/vlans/", json={"vlan_id": 200 + i, "name": f"S{i}", "devices": ["mock_device"]})

    log_all = _audit_items(admin_client)
    log_skipped = _audit_items(admin_client, skip=len(log_all))
    assert log_skipped == []


# --- Record structure ---

def test_audit_record_has_required_fields(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})

    log = _audit_items(admin_client)
    entry = next(e for e in log if e["action"] == "crear_vlan")
    for field in ("id", "timestamp", "user", "action", "resource", "details", "status"):
        assert field in entry


# --- Validation error auditing ---

def test_validation_error_is_audited(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 5000, "name": "X", "devices": ["d"]})

    log = _audit_items(admin_client)
    entry = next((e for e in log if e["action"] == "validation_error"), None)
    assert entry is not None
    assert entry["resource"] == "request"
    assert entry["status"] == "failure"
    assert "errors" in entry["details"]


def test_delete_nonexistent_vlan_audited_as_failure(admin_client):
    # VLAN.aplicar() treats deleting a VLAN that doesn't exist on the device
    # as a no-op success (reconciliar() sees existed=False and never calls
    # the driver at all) -- so to get a genuine device-rejected failure we
    # need a VLAN that DOES exist in the mock's table (30/"VOICE", see
    # vendors/mock.py's _INITIAL_MOCK_VLANS) on fail_device, whose
    # delete_vlan() unconditionally returns rc=1.
    admin_client.request("DELETE", "/api/v1/vlans/30", json={"devices": ["fail_device"]})

    log = _audit_items(admin_client)
    # A failed event's action falls back to the generic DomainEvent type
    # ("recurso_fallido") -- payload["accion"] (AuditRecord.desde()) is only
    # present on the success path, so we can't filter by "eliminar_vlan"
    # here. summary still carries "Delete VLAN 30" either way.
    entries = _entries_for_vlan(log, 30)
    assert len(entries) == 1
    assert entries[0]["status"] == "failure"
    assert entries[0]["resource"] == "vlan"
    assert entries[0]["action"] == "recurso_fallido"


# --- DB persistence ---

def test_audit_log_persists_in_db(operator_client, admin_client):
    """Records are written to DB — direct query proves it is not in-memory."""
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 77, "name": "PERSIST", "devices": ["mock_device"]})

    from app.db.models import AuditLogModel
    with get_session() as session:
        count = session.query(AuditLogModel).filter_by(action="crear_vlan").count()
    assert count > 0


def test_audit_survives_service_reimport(operator_client, admin_client):
    """A brand new ``AuditRepository()`` instance (not the composition-root
    singleton the rest of this file uses) can read back a row written
    through the singleton -- proves persistence lives in the DB, not in
    any repository-instance state. audit_service's in-memory-list era
    (where ``importlib.reload`` would actually have wiped the log) is
    gone; reloading the module itself proved flaky here (it re-executes
    module-level class definitions while the composition-root singleton
    keeps referencing the pre-reload class, which occasionally raced with
    other connections in this same session) without adding any real
    signal beyond what a fresh instance already proves."""
    from app.repositories.audit_repository import AuditRepository

    operator_client.post("/api/v1/vlans/", json={"vlan_id": 88, "name": "SURVIVE", "devices": ["mock_device"]})

    fresh_repo = AuditRepository()
    from app.models.visibility_scope import VisibilityScope
    records, _ = fresh_repo.query(scope=VisibilityScope(es_system_admin=True, grants=()), action="crear_vlan")
    assert any("VLAN 88" in (r.summary or "") for r in records)


# ── AUD-002: date range filter ─────────────────────────────────────────────────

def test_filter_by_from_date_excludes_old_records(admin_client):
    """from_date in the future returns no records."""
    audit_repository.append(AuditRecord(user="admin", action="crear_vlan", resource="vlan", details={"vlan_id": 1}))

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    log = _audit_items(admin_client, from_date=future.isoformat())
    assert log == []


def test_filter_by_to_date_excludes_new_records(admin_client):
    """to_date before the record was created returns no records."""
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    audit_repository.append(AuditRecord(user="admin", action="crear_vlan", resource="vlan", details={"vlan_id": 2}))

    log = _audit_items(admin_client, to_date=past.isoformat())
    assert log == []


def test_filter_date_range_inclusive(admin_client):
    """Records created inside the window are returned; the range is inclusive."""
    before = datetime.now(timezone.utc) - timedelta(seconds=5)
    audit_repository.append(AuditRecord(user="admin", action="crear_vlan", resource="vlan", details={"vlan_id": 3}))
    after = datetime.now(timezone.utc) + timedelta(seconds=5)

    log = _audit_items(admin_client, from_date=before.isoformat(), to_date=after.isoformat())
    assert len(log) >= 1
    assert any(e["action"] == "crear_vlan" for e in log)


def test_filter_from_date_after_to_date_returns_422(admin_client):
    """`from_date` after `to_date` is rejected with 422."""
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    response = admin_client.get(
        "/api/v1/audit/",
        params={"from_date": future.isoformat(), "to_date": past.isoformat()},
    )
    assert response.status_code == 422


def test_filter_invalid_date_string_returns_422(admin_client):
    """A non-ISO date string is rejected at the schema level with 422."""
    response = admin_client.get("/api/v1/audit/", params={"from_date": "not-a-date"})
    assert response.status_code == 422


def test_filter_omit_both_dates_returns_all(admin_client):
    """Omitting both date params returns all records (no filtering)."""
    audit_repository.append(AuditRecord(user="admin", action="crear_vlan", resource="vlan", details={"vlan_id": 4}))
    audit_repository.append(AuditRecord(user="admin", action="eliminar_vlan", resource="vlan", details={"vlan_id": 5}))

    log = _audit_items(admin_client)
    assert len(log) >= 2


# ── AUD-003: device_id filter ─────────────────────────────────────────────────

def test_filter_by_device_id(operator_client, admin_client):
    """device_id filter returns only records for that device."""
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 60, "name": "DEV_A", "devices": ["mock_device"]})
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 61, "name": "DEV_B", "devices": ["fail_device"]})

    log = _audit_items(admin_client, device_id="mock_device")
    assert len(log) >= 1
    assert all(e["device"] == "mock_device" for e in log)
    assert not any(e["device"] == "fail_device" for e in log)


def test_filter_unknown_device_returns_empty_list(admin_client):
    """An unknown device_id returns an empty list, not 404."""
    audit_repository.append(AuditRecord(
        user="admin", action="crear_vlan", resource="vlan", details={"vlan_id": 6}, device="mock_device",
    ))

    log = _audit_items(admin_client, device_id="no_such_device")
    assert log == []


def test_filter_combined_device_and_date_range(admin_client):
    """device_id and date range filters combine with AND logic."""
    before = datetime.now(timezone.utc) - timedelta(seconds=5)
    audit_repository.append(AuditRecord(
        user="admin", action="crear_vlan", resource="vlan", details={"vlan_id": 7}, device="mock_device",
    ))
    audit_repository.append(AuditRecord(
        user="admin", action="crear_vlan", resource="vlan", details={"vlan_id": 8}, device="fail_device",
    ))
    after = datetime.now(timezone.utc) + timedelta(seconds=5)

    log = _audit_items(
        admin_client, device_id="mock_device",
        from_date=before.isoformat(), to_date=after.isoformat(),
    )
    assert len(log) >= 1
    assert all(e["device"] == "mock_device" for e in log)
    assert not any(e["device"] == "fail_device" for e in log)


# ── AUD-001: append-only audit log ────────────────────────────────────────────
#
# The 3 tests that used to live here (test_status_progression_creates_separate_rows,
# test_follow_up_row_links_to_parent, test_follow_up_row_inherits_fields_from_parent)
# asserted a pending-row + parent_audit_id-linked follow-up row chain for a
# single VLAN create. Live-verified (see module docstring) that this chain
# no longer exists: Orquestador.ejecutar() dispatches exactly one terminal
# DomainEvent, so AuditListener writes exactly one row per VLAN operation,
# with parent_audit_id always None. Dropped rather than ported -- there is
# no 2-row shape left to assert against.
#
# Likewise test_append_audit_event_merges_details tested
# audit_service.append_audit_event(), a free function with no successor
# (the task description confirms this: "There is NO direct replacement for
# the old append_audit_event(...) 'follow-up row' helper"). Dropped.

def test_no_update_issued_on_audit_log(admin_client):
    """Direct UPDATE on audit_logs must be refused.

    The Step-6 app-layer guard (``AuditImmutabilityError``) fires first on any
    backend; older SQLite-only installs would have raised ``OperationalError``
    / ``IntegrityError`` from the BEFORE UPDATE trigger instead. Accept either
    so the test passes against both code paths."""
    from app.db.audit_guard import AuditImmutabilityError
    from app.db.models import AuditLogModel
    import sqlalchemy.exc

    record = audit_repository.append(AuditRecord(user="admin", action="test_action", resource="test", details={}))

    with pytest.raises((AuditImmutabilityError, sqlalchemy.exc.OperationalError, sqlalchemy.exc.IntegrityError)):
        with get_session() as session:
            row = session.query(AuditLogModel).filter_by(id=int(record.id)).first()
            row.status = "tampered"
