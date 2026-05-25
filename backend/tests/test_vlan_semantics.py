"""Tests for create/update VLAN operation semantics (existence validation)."""
import time

import pytest

from app.models.vlan import VLANInfo
from app.services import audit_service, job_service, vlan_service


@pytest.fixture(autouse=True)
def clear_log():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


# ── create_vlan ───────────────────────────────────────────────────────────────

def test_create_existing_vlan_fails(operator_client, client, monkeypatch):
    """create_vlan must fail when VLAN already exists with a different name."""
    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")
    # VLAN 50 exists on device with name "OLD_NAME"; request uses "DUPLICATE"
    monkeypatch.setattr(vlan_service, "get_vlans",
                        lambda device_id=None: [VLANInfo(vlan_id=50, name="OLD_NAME")])

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 50, "name": "DUPLICATE", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert "already exists" in job["error"]

    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.status == "failed"
    assert entry.details.get("validation") == "failed"
    assert entry.details.get("reason") == "vlan_already_exists"


def test_create_existing_vlan_same_name_noop(operator_client, client, monkeypatch):
    """create_vlan must succeed as a no-op when the VLAN already has the requested name."""
    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")
    # VLAN 50 already exists with exactly the same name being requested
    monkeypatch.setattr(vlan_service, "get_vlans",
                        lambda device_id=None: [VLANInfo(vlan_id=50, name="MGMT50")])

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 50, "name": "MGMT50", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"

    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.status == "completed"
    assert entry.details.get("reason") == "vlan_already_exists_no_op"


def test_create_new_vlan_succeeds(operator_client, client):
    """create_vlan must succeed when VLAN does not exist on the device."""
    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 800, "name": "NEW", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"


# ── update_vlan ───────────────────────────────────────────────────────────────

def test_update_nonexistent_vlan_fails(operator_client, client):
    """update_vlan must fail when VLAN does not exist on the device."""
    # VLAN 999 is not in mock_vlans [10, 20, 30]
    response = operator_client.patch(
        "/api/v1/vlans/999", json={"description": "Should fail", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    # BackgroundTasks run synchronously in TestClient
    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert "does not exist" in job["error"]

    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.status == "failed"
    assert entry.details.get("validation") == "failed"
    assert entry.details.get("reason") == "vlan_not_found"


def test_update_existing_vlan_succeeds(operator_client, client):
    """update_vlan must succeed when VLAN exists on the device."""
    # VLAN 10 exists in mock_vlans
    response = operator_client.patch(
        "/api/v1/vlans/10", json={"description": "Updated name", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    # BackgroundTasks run synchronously in TestClient
    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"


# ── pre-state unknown (existed=None) ──────────────────────────────────────────

def _patch_unknown_state(monkeypatch):
    """Make _capture_pre_state_vlan return {existed: None} for every call."""
    import app.api.vlans as vlans_module
    monkeypatch.setattr(
        vlans_module,
        "_capture_pre_state_vlan",
        lambda vlan_id, device: {"existed": None, "vlan_data": None},
    )


def test_create_aborts_when_prestate_unknown(operator_client, client, monkeypatch):
    """create_vlan must fail immediately when VLAN state cannot be determined."""
    _patch_unknown_state(monkeypatch)

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 700, "name": "UNKNOWN", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["retry_count"] == 0
    assert job["rollback_performed"] is False
    assert "aborting" in job["error"]

    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.status == "failed"
    assert entry.details["error"]["type"] == "precheck_failed"
    assert entry.details["error_type"] == "permanent"
    assert entry.details["pre_state"] == {"existed": None, "vlan_data": None}


def test_delete_aborts_when_prestate_unknown(client, monkeypatch):
    """delete_vlan must fail immediately when VLAN state cannot be determined."""
    _patch_unknown_state(monkeypatch)

    response = client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["retry_count"] == 0
    assert job["rollback_performed"] is False
    assert "aborting" in job["error"]

    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.status == "failed"
    assert entry.details["error"]["type"] == "precheck_failed"
    assert entry.details["error_type"] == "permanent"


def test_update_aborts_when_prestate_unknown(operator_client, client, monkeypatch):
    """update_vlan must fail immediately when VLAN state cannot be determined."""
    _patch_unknown_state(monkeypatch)

    response = operator_client.patch(
        "/api/v1/vlans/10", json={"description": "Anything", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    # BackgroundTasks run synchronously in TestClient
    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["retry_count"] == 0
    assert job["rollback_performed"] is False
    assert "aborting" in job["error"]

    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.status == "failed"
    assert entry.details["error"]["type"] == "precheck_failed"
    assert entry.details["error_type"] == "permanent"


def test_ansible_not_called_when_prestate_unknown(operator_client, client, monkeypatch):
    """Ansible must not be invoked when pre-state check fails."""
    _patch_unknown_state(monkeypatch)
    from app.services import vlan_service as vs

    ansible_called = [False]

    def track_create(vlan_id, name, device_id):
        ansible_called[0] = True
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vs, "create_vlan_on_device", track_create)

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 701, "name": "NOOP", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    assert ansible_called[0] is False
    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
