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
    assert entry["status"] == "success"
    assert entry["details"]["vlan_id"] == 50
    assert entry["details"]["name"] == "TEST"
    assert entry["job_id"] is not None


def test_delete_vlan_is_audited(admin_client):
    admin_client.delete("/api/v1/vlans/10?device=mock_device")

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "delete_vlan")
    assert entry["resource"] == "vlan"
    assert entry["user"] == "admin"
    assert entry["details"]["vlan_id"] == 10
    assert entry["job_id"] is not None


def test_update_vlan_is_audited(operator_client, admin_client):
    operator_client.patch("/api/v1/vlans/10", json={"description": "Core VLAN", "device": "mock_device"})

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "update_vlan")
    assert entry["resource"] == "vlan"
    assert entry["user"] == "operator"
    assert entry["details"]["description"] == "Core VLAN"
    assert entry["job_id"] is not None


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


# --- Record structure ---

def test_audit_record_has_required_fields(operator_client, admin_client):
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]})

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "create_vlan")
    for field in ("id", "timestamp", "user", "action", "resource", "details", "status"):
        assert field in entry
