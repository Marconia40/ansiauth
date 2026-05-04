import time

import pytest

from app.services import audit_service


@pytest.fixture(autouse=True)
def clear_log():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


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
    entries = [e for e in log if e["action"] == "create_vlan"]
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
    admin_client.delete("/api/v1/vlans/10?device=mock_device")

    # BackgroundTasks run synchronously in TestClient, so status is final immediately
    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "delete_vlan" and e["job_id"] is not None)
    assert entry["resource"] == "vlan"
    assert entry["user"] == "admin"
    assert entry["details"]["vlan_id"] == 10
    assert entry["job_id"] is not None
    assert entry["status"] == "completed"


def test_update_vlan_is_audited(operator_client, admin_client):
    operator_client.patch("/api/v1/vlans/10", json={"description": "Core VLAN", "device": "mock_device"})

    # BackgroundTasks run synchronously in TestClient, so status is final immediately
    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "update_vlan")
    assert entry["resource"] == "vlan"
    assert entry["user"] == "operator"
    assert entry["details"]["description"] == "Core VLAN"
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
    admin_client.delete("/api/v1/vlans/10?device=mock_device")

    log = admin_client.get("/api/v1/audit/?user=operator").json()
    assert all(e["user"] == "operator" for e in log)
    assert any(e["action"] == "create_vlan" for e in log)


def test_filter_by_action(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})
    admin_client.delete("/api/v1/vlans/10?device=mock_device")

    log = admin_client.get("/api/v1/audit/?action=create_vlan").json()
    assert all(e["action"] == "create_vlan" for e in log)


def test_filter_by_resource(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})
    admin_client.delete("/api/v1/vlans/10?device=mock_device")
    unauth_client_resp = operator_client.patch("/api/v1/vlans/10", json={"description": "x", "device": "mock_device"})

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
    admin_client.delete("/api/v1/vlans/99?device=mock_device")

    log = admin_client.get("/api/v1/audit/").json()
    entry = next((e for e in log if e["action"] == "delete_vlan" and e["status"] == "failure"), None)
    assert entry is not None
    assert entry["resource"] == "vlan"
    assert entry["details"]["vlan_id"] == 99
    assert entry["details"]["error"] == "VLAN does not exist"


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
