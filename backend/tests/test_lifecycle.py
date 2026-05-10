"""Tests for job/audit lifecycle consistency (no stuck pending states)."""
import time

import pytest

from app.services import audit_service, job_service, vlan_service


@pytest.fixture(autouse=True)
def clear_log():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


# ── Test 1: Successful execution ──────────────────────────────────────────────

def test_successful_create_ends_completed(operator_client, admin_client):
    """Job and audit must both reach 'completed' on success."""
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 601, "name": "OK", "devices": ["mock_device"]})

    time.sleep(0.5)

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "create_vlan" and e["details"].get("vlan_id") == 601)
    assert entry["status"] == "completed"
    assert entry["details"].get("duration_seconds") is not None

    job = job_service.get_job(entry["job_id"])
    assert job is not None
    assert job.status == "completed"
    assert job.finished_at is not None


# ── Test 2: Duplicate VLAN ────────────────────────────────────────────────────

def test_duplicate_vlan_ends_failed_no_pending(operator_client, admin_client, monkeypatch):
    """When Ansible reports a duplicate VLAN the job must end in failed, never stuck in pending."""
    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 1, "stdout": "", "stderr": "VLAN already exists"})

    operator_client.post("/api/v1/vlans/", json={"vlan_id": 650, "name": "DUP", "devices": ["mock_device"]})

    time.sleep(0.5)

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(
        (e for e in log if e["action"] == "create_vlan" and e["details"].get("vlan_id") == 650),
        None,
    )
    assert entry is not None
    assert entry["status"] == "failed", f"Expected failed, got {entry['status']}"

    job = job_service.get_job(entry["job_id"])
    assert job is not None
    assert job.status == "failed"
    assert job.status != "pending"


# ── Test 3: SSH / transient failure ──────────────────────────────────────────

def test_ssh_failure_updates_retry_count(operator_client, admin_client, monkeypatch):
    """SSH failure triggers retries and the final failed job shows retry_count > 0."""
    import app.api.vlans as vlans_module
    monkeypatch.setattr(vlans_module, "_RETRY_BASE_DELAY", 0.001)
    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 1, "stdout": "", "stderr": "SSH connection timeout"})

    operator_client.post("/api/v1/vlans/", json={"vlan_id": 602, "name": "SSH", "devices": ["mock_device"]})

    time.sleep(0.5)

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "create_vlan" and e["details"].get("vlan_id") == 602)
    assert entry["status"] == "failed"
    assert entry["details"].get("retries", 0) > 0

    job = job_service.get_job(entry["job_id"])
    assert job.status == "failed"
    assert job.retry_count > 0


# ── Test 4: Unexpected crash / ensure_final_state ─────────────────────────────

def test_ensure_final_state_clears_stuck_pending():
    """ensure_final_state must force any non-terminal job to failed."""
    job = job_service.create_job(playbook="test.yml", device="phantom")
    assert job.status == "pending"

    job_service.ensure_final_state(job.job_id)

    updated = job_service.get_job(job.job_id)
    assert updated.status == "failed"
    assert updated.error == "Unexpected termination"
    assert updated.finished_at is not None


def test_ensure_final_state_does_not_touch_completed():
    """ensure_final_state must not overwrite an already completed job."""
    job = job_service.create_job()
    job_service.update_job(job.job_id, "completed", result={"output": "ok"})

    job_service.ensure_final_state(job.job_id)

    updated = job_service.get_job(job.job_id)
    assert updated.status == "completed"


def test_ensure_final_state_clears_stuck_running():
    """ensure_final_state must also clear 'running' if somehow stuck."""
    job = job_service.create_job()
    job_service.update_job(job.job_id, "running")

    job_service.ensure_final_state(job.job_id)

    updated = job_service.get_job(job.job_id)
    assert updated.status == "failed"


def test_ensure_audit_final_state_clears_stuck_pending(admin_client):
    """ensure_audit_final_state must force a pending audit to failed."""
    record = audit_service.log_action(
        user="test", action="test_action", resource="test",
        details={}, status="pending",
    )

    audit_service.ensure_audit_final_state(record.id)

    # Append-only: the original row stays "pending"; a new follow-up row is inserted.
    from app.db.models import AuditLogModel
    from app.db.session import get_session
    with get_session() as session:
        follow_up = (
            session.query(AuditLogModel)
            .filter_by(parent_audit_id=int(record.id), status="failed")
            .first()
        )
        assert follow_up is not None
        details = dict(follow_up.details or {})
    assert details.get("error", {}).get("type") == "unexpected_termination"


# ── Test 5: Structured error in audit ────────────────────────────────────────

def test_failed_job_has_structured_error_in_audit(operator_client, admin_client, monkeypatch):
    """Audit details must contain a structured error dict on ansible failure."""
    import app.api.vlans as vlans_module
    monkeypatch.setattr(vlans_module, "_RETRY_BASE_DELAY", 0.01)
    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 1, "stdout": "", "stderr": "device unreachable"})

    operator_client.post("/api/v1/vlans/", json={"vlan_id": 603, "name": "ERR", "devices": ["mock_device"]})

    time.sleep(0.5)

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "create_vlan" and e["details"].get("vlan_id") == 603)
    assert entry["status"] == "failed"
    err = entry["details"].get("error")
    assert err is not None
    assert err["type"] == "ansible_error"
    assert err["rc"] == 1
    assert "unreachable" in err["stderr"]


# ── Test 6: Jobs endpoint consistency ────────────────────────────────────────

def test_jobs_endpoint_returns_all_created_jobs(operator_client, client):
    """Every job created via the VLAN API must appear in /jobs/."""
    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 604, "name": "JOBCHECK", "devices": ["mock_device", "mock_device"]}
    )
    assert response.status_code == 200
    created_ids = {e["job_id"] for e in response.json()["jobs"]}

    all_jobs = {j["job_id"] for j in client.get("/api/v1/jobs/").json()["data"]}
    assert created_ids.issubset(all_jobs)


def test_job_fetchable_by_id_immediately_after_creation(operator_client, client):
    """Job must be retrievable by ID before execution completes."""
    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 605, "name": "IMMEDIATE", "devices": ["mock_device"]}
    )
    job_id = response.json()["jobs"][0]["job_id"]

    resp = client.get(f"/api/v1/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["job_id"] == job_id
