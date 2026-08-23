"""Tests for smart retry logic based on error classification."""
import time

import pytest

from app.models.vlan import VLAN

import app.api.vlans as vlans_module
from app.services import ansible_service, audit_service, job_service, vlan_service


@pytest.fixture(autouse=True)
def clear_log():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """Speed up retry delays so tests run in milliseconds."""
    monkeypatch.setattr(vlans_module, "_RETRY_BASE_DELAY", 0.01)


# ── Test 1: SSH/transient error → retry exhausted ─────────────────────────────

def test_ssh_failure_retries_and_fails(operator_client, client, monkeypatch):
    """Transient SSH errors must cause 3 retries; final status must be failed."""
    call_count = {"n": 0}

    def _ssh_failure(playbook, extravars, inventory=None, device=None):
        call_count["n"] += 1
        return {"rc": 1, "stdout": "", "stderr": "SSH connection refused to host"}

    monkeypatch.setattr(ansible_service, "run_playbook", _ssh_failure)
    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")
    monkeypatch.setattr(vlan_service, "vlan_exists", lambda device_id, vlan_id: False)

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 100, "name": "SSH_TEST", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["retry_count"] == 3
    # 4 create attempts (1 initial + 3 retries) + 1 rollback delete attempt = 5
    assert call_count["n"] == 5
    # Rollback was attempted but the delete also failed (SSH still down)
    assert job["rollback_performed"] is True
    assert job["rollback_success"] is False

    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.status == "failed"
    assert entry.details.get("error_type") == "transient"
    assert entry.details.get("retries") == 3


# ── Test 2: Permanent error (validation) → immediate fail, no retries ─────────

def test_duplicate_vlan_fails_immediately_no_retries(operator_client, client, monkeypatch):
    """A duplicate VLAN (exists with different name) must fail immediately with retry_count=0."""
    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")
    monkeypatch.setattr(vlan_service, "get_vlans",
                        lambda device_id=None: [VLAN(vlan_id=50, name="EXISTING_NAME")])

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 50, "name": "DUPLICATE", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.3)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["retry_count"] == 0
    assert "already exists" in job["error"]

    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.status == "failed"
    assert entry.details.get("validation") == "failed"
    assert entry.details.get("reason") == "vlan_already_exists"


# ── Test 3: Normal execution → success, no retries ────────────────────────────

def test_normal_execution_succeeds_no_retries(operator_client, client):
    """A successful Ansible run must complete with retry_count=0 and status=completed."""
    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 200, "name": "OK_VLAN", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.3)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    assert job["retry_count"] == 0

    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.status == "completed"
    assert entry.details.get("retries") == 0


# ── Test 4: Non-transient Ansible error → no retries, permanent error_type ────

def test_permanent_ansible_error_no_retries(operator_client, client, monkeypatch):
    """A non-transient Ansible error must fail immediately with error_type=permanent."""
    call_count = {"n": 0}

    def _permanent_failure(playbook, extravars, inventory=None, device=None):
        call_count["n"] += 1
        return {"rc": 1, "stdout": "", "stderr": "VLAN configuration syntax error"}

    monkeypatch.setattr(ansible_service, "run_playbook", _permanent_failure)
    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")
    monkeypatch.setattr(vlan_service, "vlan_exists", lambda device_id, vlan_id: False)

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 300, "name": "PERM_FAIL", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.3)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["retry_count"] == 0
    # 1 create attempt (no retries — permanent error) + 1 rollback delete attempt = 2
    assert call_count["n"] == 2
    # Rollback was attempted but the delete also failed (same Ansible failure)
    assert job["rollback_performed"] is True
    assert job["rollback_success"] is False

    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.status == "failed"
    assert entry.details.get("error_type") == "permanent"
    assert entry.details.get("retries") == 0


# ── Test 5: Retry count visible in real time via /jobs/{id} ───────────────────

def test_retry_count_visible_during_execution(operator_client, client, monkeypatch):
    """retry_count and last_error must reflect all retry attempts in the final job state."""
    import app.api.vlans as vlans_module

    call_number = {"n": 0}

    def _transient_then_succeed(playbook, extravars, inventory=None, device=None):
        call_number["n"] += 1
        n = call_number["n"]
        # Fail the first 2 attempts with a transient error, then succeed
        if n <= 2:
            return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(ansible_service, "run_playbook", _transient_then_succeed)
    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")
    monkeypatch.setattr(vlans_module, "_RETRY_BASE_DELAY", 0.05)

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 400, "name": "RETRY_VIS", "devices": ["mock_device"]}
    )
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    # BackgroundTasks run synchronously in TestClient, so the job is already
    # complete by the time client.post() returns.
    final = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert final["status"] == "completed"
    assert final["retry_count"] == 2
    assert final["last_error"] is not None
    assert "ssh" in final["last_error"].lower()
    # 2 failing attempts + 1 successful attempt
    assert call_number["n"] == 3
