"""Tests for safe VLAN rollback using real pre-state."""
import time

import pytest

from app.services import audit_service, vlan_service


@pytest.fixture(autouse=True)
def clear_log():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


# ── CREATE: forced failure → rollback deletes the VLAN ───────────────────────

def test_create_failure_triggers_rollback(client, monkeypatch):
    """If create_vlan fails and VLAN did not previously exist, a delete rollback must run."""
    rollback_calls: list = []

    def _fail_create(vlan_id, name, device_id):
        return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}

    def _track_delete(vlan_id, device_id):
        rollback_calls.append(vlan_id)
        return {"rc": 0, "stdout": "rolled back", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", _fail_create)
    monkeypatch.setattr(vlan_service, "delete_vlan", _track_delete)

    response = client.post(
        "/api/v1/vlans/", json={"vlan_id": 901, "name": "ROLLBACK_TEST", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["rollback_performed"] is True
    assert 901 in rollback_calls

    # pre_state must be attached to the job
    assert job["pre_state"] is not None
    assert job["pre_state"]["existed"] is False

    # Audit must record rollback and pre_state
    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.details.get("rollback_performed") is True
    assert entry.details.get("pre_state") is not None


# ── UPDATE: forced failure → old name is restored ────────────────────────────

def test_update_failure_restores_previous_name(client, monkeypatch):
    """If update_vlan fails, rollback must restore the VLAN's original name."""
    call_log: list[dict] = []

    def _track_update(vlan_id, description, device_id):
        call_log.append({"vlan_id": vlan_id, "description": description})
        # First call (the real update) fails with a permanent error; rollback call succeeds
        if len(call_log) == 1:
            return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}
        return {"rc": 0, "stdout": "restored", "stderr": ""}

    monkeypatch.setattr(vlan_service, "update_vlan_description", _track_update)

    # VLAN 10 exists in _mock_vlans with name "MGMT"
    response = client.patch(
        "/api/v1/vlans/10", json={"description": "NEW_NAME", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["rollback_performed"] is True

    # Second call must have restored the original name
    assert len(call_log) == 2
    assert call_log[1]["description"] == "MGMT"

    # pre_state must record what the VLAN looked like before the update
    assert job["pre_state"] is not None
    assert job["pre_state"]["existed"] is True
    assert job["pre_state"]["vlan_data"]["name"] == "MGMT"

    # Audit must record rollback and pre_state
    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.details.get("rollback_performed") is True
    assert entry.details.get("pre_state", {}).get("vlan_data", {}).get("name") == "MGMT"


# ── DELETE: forced failure → VLAN is recreated ────────────────────────────────

def test_delete_failure_recreates_vlan(client, admin_client, monkeypatch):
    """If delete_vlan fails and VLAN existed before, rollback must recreate it."""
    recreate_calls: list = []

    def _fail_delete(vlan_id, device_id):
        return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}

    def _track_create(vlan_id, name, device_id):
        recreate_calls.append({"vlan_id": vlan_id, "name": name})
        return {"rc": 0, "stdout": "recreated", "stderr": ""}

    monkeypatch.setattr(vlan_service, "delete_vlan", _fail_delete)
    monkeypatch.setattr(vlan_service, "create_vlan_on_device", _track_create)

    # VLAN 10 exists in _mock_vlans with name "MGMT"
    response = admin_client.request(
        "DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["rollback_performed"] is True

    # Rollback must have recreated the VLAN with the original name
    assert len(recreate_calls) == 1
    assert recreate_calls[0]["vlan_id"] == 10
    assert recreate_calls[0]["name"] == "MGMT"

    # pre_state must record that the VLAN existed with name "MGMT"
    assert job["pre_state"] is not None
    assert job["pre_state"]["existed"] is True
    assert job["pre_state"]["vlan_data"]["name"] == "MGMT"

    # Audit must record rollback and pre_state
    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.details.get("rollback_performed") is True
    assert entry.details.get("pre_state", {}).get("existed") is True


# ── pre_state visible on successful jobs too ─────────────────────────────────

def test_pre_state_present_on_successful_create(client):
    """pre_state must be attached to the job even on successful operations."""
    response = client.post(
        "/api/v1/vlans/", json={"vlan_id": 902, "name": "NEW_VLAN", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    # VLAN 902 was not in _mock_vlans, so pre_state should show existed=False
    assert job["pre_state"] is not None
    assert job["pre_state"]["existed"] is False
