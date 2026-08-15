import time
from datetime import datetime, timezone, timedelta

import pytest

from app.schemas.user import UserCreate
from app.services import audit_service, user_service


@pytest.fixture(autouse=True)
def clear_log():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


@pytest.fixture(autouse=True)
def seed_users():
    """Ensure test accounts exist for login-audit tests."""
    for username, password, role in [
        ("operator", "operator123", "operator"),
    ]:
        if user_service.get_by_username(username) is None:
            user_service.create_user(
                UserCreate(username=username, password=password, role=role)
            )


# --- Access control ---

def test_audit_log_requires_admin(observer_client, operator_client):
    assert observer_client.get("/api/v1/audit/").status_code == 403
    assert operator_client.get("/api/v1/audit/").status_code == 403


def test_audit_log_without_token(unauth_client):
    assert unauth_client.get("/api/v1/audit/").status_code == 401


def test_audit_log_accessible_by_admin(admin_client):
    response = admin_client.get("/api/v1/audit/")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


# --- VLAN auditing ---

def test_create_vlan_is_audited(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "create_vlan")
    assert entry["resource"] == "vlan"
    assert entry["user"] == "operator"
    assert entry["details"]["vlan_id"] == 50
    assert entry["details"]["name"] == "TEST"
    assert entry["job_id"] is not None
    assert entry["device"] == "mock_device"
    assert entry["request_id"] is not None

    # After background thread finishes, status must reflect execution result
    time.sleep(1)
    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "create_vlan")
    assert entry["status"] == "completed"


def test_create_vlan_multi_device_audit_rows(operator_client, admin_client):
    operator_client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 51, "name": "MULTI", "devices": ["mock_device", "mock_device"]},
    )

    time.sleep(1)
    log = admin_client.get("/api/v1/audit/").json()
    # With append-only each device produces 2 rows (pending + completed follow-up).
    # Filter to follow-up events (parent_audit_id set) to count one outcome per device.
    entries = [e for e in log if e["action"] == "create_vlan" and e["parent_audit_id"] is not None]
    assert len(entries) == 2

    for entry in entries:
        assert entry["device"] == "mock_device"
        assert entry["job_id"] is not None
        assert entry["request_id"] is not None
        assert entry["status"] == "completed"

    # All entries for this request share the same request_id
    request_ids = {e["request_id"] for e in entries}
    assert len(request_ids) == 1

    # Each entry links to a distinct job
    job_ids = {e["job_id"] for e in entries}
    assert len(job_ids) == 2


def test_audit_status_updates_after_execution(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 52, "name": "STATUSTEST", "devices": ["mock_device"]})

    time.sleep(1)
    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "create_vlan" and e["details"]["vlan_id"] == 52)
    assert entry["status"] == "completed"


def test_audit_status_failed_on_device_error(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 53, "name": "FAILAUDIT", "devices": ["fail_device"]})

    time.sleep(1)
    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "create_vlan" and e["details"]["vlan_id"] == 53)
    assert entry["status"] == "failed"
    assert entry["device"] == "fail_device"


def test_delete_vlan_is_audited(admin_client):
    admin_client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})

    # BackgroundTasks run synchronously in TestClient, so status is final immediately
    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "delete_vlan" and e["job_id"] is not None)
    assert entry["resource"] == "vlan"
    assert entry["user"] == "admin"
    assert entry["details"]["vlan_id"] == 10
    assert entry["job_id"] is not None
    assert entry["status"] == "completed"


def test_update_vlan_is_audited(operator_client, admin_client):
    operator_client.patch("/api/v1/vlans/10", json={"description": "Core-VLAN", "devices": ["mock_device"]})

    # BackgroundTasks run synchronously in TestClient, so status is final immediately
    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "update_vlan")
    assert entry["resource"] == "vlan"
    assert entry["user"] == "operator"
    assert entry["details"]["description"] == "Core-VLAN"
    assert entry["job_id"] is not None
    assert entry["status"] == "completed"


# --- Jobs auditing ---

def test_cancel_job_is_audited(admin_client):
    from app.services import job_service
    job = job_service.create_job()

    admin_client.post(f"/api/v1/jobs/{job.job_id}/cancel")

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "cancel_job")
    assert entry["resource"] == "job"
    assert entry["user"] == "admin"
    assert entry["details"]["job_id"] == job.job_id


# --- Auth auditing ---

def test_login_success_is_audited(unauth_client, admin_client):
    unauth_client.post("/api/v1/auth/login", data={"username": "operator", "password": "operator123"})

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "login" and e["status"] == "success")
    assert entry["resource"] == "auth"
    assert entry["user"] == "operator"


def test_login_failure_is_audited(unauth_client, admin_client):
    unauth_client.post("/api/v1/auth/login", data={"username": "operator", "password": "wrongpass"})

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "login" and e["status"] == "failed")
    assert entry["resource"] == "auth"
    assert entry["user"] == "operator"


# --- Filters ---

def test_filter_by_user(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})
    admin_client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})

    log = admin_client.get("/api/v1/audit/?user=operator").json()
    assert all(e["user"] == "operator" for e in log)
    assert any(e["action"] == "create_vlan" for e in log)


def test_filter_by_action(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})
    admin_client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})

    log = admin_client.get("/api/v1/audit/?action=create_vlan").json()
    assert all(e["action"] == "create_vlan" for e in log)


def test_filter_by_resource(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})
    admin_client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})
    operator_client.patch("/api/v1/vlans/10", json={"description": "x", "devices": ["mock_device"]})

    log = admin_client.get("/api/v1/audit/?resource=vlan").json()
    assert len(log) > 0
    assert all(e["resource"] == "vlan" for e in log)


# --- Pagination ---

def test_pagination_limit(operator_client, admin_client):
    for i in range(5):
        operator_client.post("/api/v1/vlans/", json={"vlan_id": 10 + i, "name": f"V{i}", "devices": ["mock_device"]})

    log_all = admin_client.get("/api/v1/audit/").json()
    log_limited = admin_client.get("/api/v1/audit/?limit=2").json()
    assert len(log_limited) == 2
    assert len(log_all) > 2


def test_pagination_skip(operator_client, admin_client):
    for i in range(4):
        operator_client.post("/api/v1/vlans/", json={"vlan_id": 20 + i, "name": f"S{i}", "devices": ["mock_device"]})

    log_all = admin_client.get("/api/v1/audit/").json()
    log_skipped = admin_client.get(f"/api/v1/audit/?skip={len(log_all)}").json()
    assert log_skipped == []


# --- Record structure ---

def test_audit_record_has_required_fields(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "create_vlan")
    for field in ("id", "timestamp", "user", "action", "resource", "details", "status"):
        assert field in entry


# --- Validation error auditing ---

def test_validation_error_is_audited(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 5000, "name": "X", "devices": ["d"]})

    log = admin_client.get("/api/v1/audit/").json()
    entry = next((e for e in log if e["action"] == "validation_error"), None)
    assert entry is not None
    assert entry["resource"] == "request"
    assert entry["status"] == "failure"
    assert "errors" in entry["details"]


def test_delete_nonexistent_vlan_audited_as_failure(admin_client):
    admin_client.request("DELETE", "/api/v1/vlans/99", json={"devices": ["mock_device"]})

    # BackgroundTasks run synchronously in TestClient — job is already failed
    log = admin_client.get("/api/v1/audit/").json()
    entry = next((e for e in log if e["action"] == "delete_vlan" and e["status"] == "failed"), None)
    assert entry is not None
    assert entry["resource"] == "vlan"
    assert entry["details"]["vlan_id"] == 99


# --- DB persistence ---

def test_audit_log_persists_in_db(operator_client, admin_client):
    """Records are written to DB — direct query proves it is not in-memory."""
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 77, "name": "PERSIST", "devices": ["mock_device"]})

    from app.db.models import AuditLogModel
    from app.db.session import get_session
    with get_session() as session:
        count = session.query(AuditLogModel).filter_by(action="create_vlan").count()
    assert count > 0


def test_audit_survives_service_reimport(operator_client, admin_client):
    """Re-importing audit_service does not wipe the log (proves no in-memory dependency)."""
    import importlib
    from app.services import audit_service as svc

    operator_client.post("/api/v1/vlans/", json={"vlan_id": 88, "name": "SURVIVE", "devices": ["mock_device"]})

    importlib.reload(svc)  # simulate module restart — in-memory list would be empty here

    log = admin_client.get("/api/v1/audit/").json()
    assert any(e["action"] == "create_vlan" for e in log)


# ── AUD-002: date range filter ─────────────────────────────────────────────────

def test_filter_by_from_date_excludes_old_records(admin_client):
    """from_date in the future returns no records."""
    audit_service.log_action("admin", "create_vlan", "vlan", {"vlan_id": 1})

    future = datetime.now(timezone.utc) + timedelta(hours=1)
    log = admin_client.get("/api/v1/audit/", params={"from_date": future.isoformat()}).json()
    assert log == []


def test_filter_by_to_date_excludes_new_records(admin_client):
    """to_date before the record was created returns no records."""
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    audit_service.log_action("admin", "create_vlan", "vlan", {"vlan_id": 2})

    log = admin_client.get("/api/v1/audit/", params={"to_date": past.isoformat()}).json()
    assert log == []


def test_filter_date_range_inclusive(admin_client):
    """Records created inside the window are returned; the range is inclusive."""
    before = datetime.now(timezone.utc) - timedelta(seconds=5)
    audit_service.log_action("admin", "create_vlan", "vlan", {"vlan_id": 3})
    after = datetime.now(timezone.utc) + timedelta(seconds=5)

    log = admin_client.get(
        "/api/v1/audit/",
        params={"from_date": before.isoformat(), "to_date": after.isoformat()},
    ).json()
    assert len(log) >= 1
    assert any(e["action"] == "create_vlan" for e in log)


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
    audit_service.log_action("admin", "create_vlan", "vlan", {"vlan_id": 4})
    audit_service.log_action("admin", "delete_vlan", "vlan", {"vlan_id": 5})

    log = admin_client.get("/api/v1/audit/").json()
    assert len(log) >= 2


# ── AUD-003: device_id filter ─────────────────────────────────────────────────

def test_filter_by_device_id(operator_client, admin_client):
    """device_id filter returns only records for that device."""
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 60, "name": "DEV_A", "devices": ["mock_device"]})
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 61, "name": "DEV_B", "devices": ["fail_device"]})

    log = admin_client.get("/api/v1/audit/", params={"device_id": "mock_device"}).json()
    assert len(log) >= 1
    assert all(e["device"] == "mock_device" for e in log)
    assert not any(e["device"] == "fail_device" for e in log)


def test_filter_unknown_device_returns_empty_list(admin_client):
    """An unknown device_id returns an empty list, not 404."""
    audit_service.log_action("admin", "create_vlan", "vlan", {"vlan_id": 6}, device="mock_device")

    log = admin_client.get("/api/v1/audit/", params={"device_id": "no_such_device"}).json()
    assert log == []


def test_filter_combined_device_and_date_range(admin_client):
    """device_id and date range filters combine with AND logic."""
    before = datetime.now(timezone.utc) - timedelta(seconds=5)
    audit_service.log_action("admin", "create_vlan", "vlan", {"vlan_id": 7}, device="mock_device")
    audit_service.log_action("admin", "create_vlan", "vlan", {"vlan_id": 8}, device="fail_device")
    after = datetime.now(timezone.utc) + timedelta(seconds=5)

    log = admin_client.get(
        "/api/v1/audit/",
        params={
            "device_id": "mock_device",
            "from_date": before.isoformat(),
            "to_date": after.isoformat(),
        },
    ).json()
    assert len(log) >= 1
    assert all(e["device"] == "mock_device" for e in log)
    assert not any(e["device"] == "fail_device" for e in log)


# ── AUD-001: append-only audit log ────────────────────────────────────────────

def test_status_progression_creates_separate_rows(operator_client, admin_client):
    """A VLAN create must produce two audit rows: initial 'pending' and follow-up terminal event."""
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 90, "name": "CHAIN", "devices": ["mock_device"]})

    log = admin_client.get("/api/v1/audit/").json()
    vlan_entries = [e for e in log if e["action"] == "create_vlan" and e["details"].get("vlan_id") == 90]

    assert len(vlan_entries) == 2
    statuses = {e["status"] for e in vlan_entries}
    assert "pending" in statuses
    assert "completed" in statuses


def test_follow_up_row_links_to_parent(operator_client, admin_client):
    """The follow-up event row must have parent_audit_id pointing to the initial row."""
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 91, "name": "LINK", "devices": ["mock_device"]})

    log = admin_client.get("/api/v1/audit/").json()
    vlan_entries = [e for e in log if e["action"] == "create_vlan" and e["details"].get("vlan_id") == 91]

    initial = next(e for e in vlan_entries if e["parent_audit_id"] is None)
    follow_up = next(e for e in vlan_entries if e["parent_audit_id"] is not None)

    assert follow_up["parent_audit_id"] == initial["id"]


def test_follow_up_row_inherits_fields_from_parent(operator_client, admin_client):
    """Follow-up rows must carry user, device, job_id, request_id copied from the initial row."""
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 92, "name": "INHERIT", "devices": ["mock_device"]})

    log = admin_client.get("/api/v1/audit/").json()
    vlan_entries = [e for e in log if e["action"] == "create_vlan" and e["details"].get("vlan_id") == 92]

    initial = next(e for e in vlan_entries if e["parent_audit_id"] is None)
    follow_up = next(e for e in vlan_entries if e["parent_audit_id"] is not None)

    for field in ("user", "action", "resource", "device", "job_id", "request_id"):
        assert follow_up[field] == initial[field], f"field {field!r} not inherited"


def test_no_update_issued_on_audit_log(operator_client, admin_client):
    """Direct UPDATE on audit_logs must be refused.

    The Step-6 app-layer guard (``AuditImmutabilityError``) fires first on any
    backend; older SQLite-only installs would have raised ``OperationalError``
    / ``IntegrityError`` from the BEFORE UPDATE trigger instead. Accept either
    so the test passes against both code paths."""
    from app.db.audit_guard import AuditImmutabilityError
    from app.db.models import AuditLogModel
    from app.db.session import get_session
    import sqlalchemy.exc

    record = audit_service.log_action("admin", "test_action", "test", {})

    with pytest.raises((AuditImmutabilityError, sqlalchemy.exc.OperationalError, sqlalchemy.exc.IntegrityError)):
        with get_session() as session:
            row = session.query(AuditLogModel).filter_by(id=int(record.id)).first()
            row.status = "tampered"


def test_append_audit_event_merges_details(admin_client):
    """append_audit_event must merge extra_details on top of the parent's details."""
    record = audit_service.log_action("admin", "create_vlan", "vlan", {"vlan_id": 93, "name": "MERGE"})

    audit_service.append_audit_event(record.id, "completed", {"retries": 0, "duration_seconds": 0.5})

    log = audit_service.get_audit_log()
    follow_up = next(
        e for e in log
        if e.parent_audit_id == record.id and e.status == "completed"
    )
    assert follow_up.details["vlan_id"] == 93
    assert follow_up.details["name"] == "MERGE"
    assert follow_up.details["retries"] == 0
    assert follow_up.details["duration_seconds"] == 0.5
