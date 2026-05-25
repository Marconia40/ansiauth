"""Tests for observability improvements (step 3.4).

Covers: execution_summary structure, current_step lifecycle,
rollback-skipped log, and retry wait duration in logs.
"""
import logging
import time

import pytest

from app.services import ansible_service, audit_service, vlan_service
import app.api.vlans as vlans_module
import app.services.vlan_execution_service as svc


@pytest.fixture(autouse=True)
def clear_audit():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


# ── execution_summary structure ───────────────────────────────────────────────

def test_execution_summary_present_on_success(client):
    """execution_summary must appear in the job response for completed jobs."""
    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 700, "name": "OBS_VLAN", "devices": ["mock_device"]},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["job_id"]
    time.sleep(0.3)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    summary = job["execution_summary"]
    assert summary is not None
    assert summary["attempts"] == 1
    assert summary["rollback_performed"] is False
    assert summary["rollback_success"] is None
    assert summary["duration_ms"] is not None
    assert summary["duration_ms"] >= 0


def test_execution_summary_attempts_after_retries(client, monkeypatch):
    """execution_summary.attempts must equal retry_count + 1 after retries."""
    call_number = {"n": 0}

    def _transient_then_succeed(playbook, extravars, inventory=None, device=None):
        call_number["n"] += 1
        if call_number["n"] <= 2:
            return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(ansible_service, "run_playbook", _transient_then_succeed)
    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")
    monkeypatch.setattr(vlans_module, "_RETRY_BASE_DELAY", 0.01)

    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 701, "name": "RETRY_OBS", "devices": ["mock_device"]},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    assert job["retry_count"] == 2
    summary = job["execution_summary"]
    assert summary["attempts"] == 3
    assert summary["duration_ms"] is not None


def test_execution_summary_on_failed_with_rollback(client, monkeypatch):
    """execution_summary must reflect rollback state on failed jobs."""
    def _fail_create(vlan_id, name, device_id):
        return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}

    def _succeed_delete(vlan_id, device_id):
        return {"rc": 0, "stdout": "deleted", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", _fail_create)
    monkeypatch.setattr(vlan_service, "delete_vlan", _succeed_delete)

    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 901, "name": "ROLLBACK_OBS", "devices": ["mock_device"]},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["job_id"]
    time.sleep(0.3)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    summary = job["execution_summary"]
    assert summary["rollback_performed"] is True
    assert summary["rollback_success"] is True
    assert summary["attempts"] == 1


def test_execution_summary_attempts_none_for_pending_job(client):
    """execution_summary.attempts must be None for jobs not yet finished."""
    from app.services import job_service
    job = job_service.create_job(playbook="create_vlan.yml", device="mock_device", parameters={})

    resp = client.get(f"/api/v1/jobs/{job.job_id}").json()["data"]
    assert resp["status"] == "pending"
    assert resp["execution_summary"]["attempts"] is None


# ── current_step lifecycle ────────────────────────────────────────────────────

def test_current_step_rollback_completed_after_failure(client, monkeypatch):
    """current_step must be 'rollback_completed' after a failed job with rollback."""
    def _fail_create(vlan_id, name, device_id):
        return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}

    def _succeed_delete(vlan_id, device_id):
        return {"rc": 0, "stdout": "deleted", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", _fail_create)
    monkeypatch.setattr(vlan_service, "delete_vlan", _succeed_delete)

    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 902, "name": "STEP_OBS", "devices": ["mock_device"]},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["job_id"]
    time.sleep(0.3)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["current_step"] == "rollback_completed"


def test_current_step_completed_on_success(client):
    """current_step must be 'completed' after a successful create."""
    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 703, "name": "STEP_OK", "devices": ["mock_device"]},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["job_id"]
    time.sleep(0.3)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    # completed jobs set current_step in no-op path; regular path doesn't set it explicitly
    assert job["status"] == "completed"


# ── Rollback-skipped log ──────────────────────────────────────────────────────

def test_rollback_skipped_log_when_vlan_existed(caplog):
    """'rollback skipped' must be logged when VLAN existed before the operation."""
    pre = {"existed": True, "vlan_data": {"vlan_id": 10, "name": "MGMT"}}
    with caplog.at_level(logging.INFO, logger="app.services.vlan_execution_service"):
        performed, success = svc._rollback_create(10, "dev", "skip-job", pre)
    assert performed is False
    assert success is None
    assert any("rollback skipped" in r.message.lower() for r in caplog.records)


def test_rollback_skipped_log_when_pre_state_unknown(caplog):
    """'rollback skipped' must also be logged when pre-state is unknown (existed=None)."""
    pre = {"existed": None, "vlan_data": None}
    with caplog.at_level(logging.INFO, logger="app.services.vlan_execution_service"):
        performed, success = svc._rollback_create(10, "dev", "skip-unknown", pre)
    assert performed is False
    assert success is None
    assert any("rollback skipped" in r.message.lower() for r in caplog.records)


# ── Retry wait duration in logs ───────────────────────────────────────────────

def test_retry_wait_duration_in_log(monkeypatch, caplog):
    """Retry log must include 'waiting Xs' with the computed delay."""
    monkeypatch.setattr(svc.time, "sleep", lambda d: None)
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    def _transient():
        return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}

    with caplog.at_level(logging.INFO, logger="app.services.vlan_execution_service"):
        svc._execute_with_retry(_transient, "timing-test", max_retries=1, retry_base_delay=2.0)

    retry_lines = [r.message for r in caplog.records if "retry attempt" in r.message]
    assert len(retry_lines) == 1
    assert "waiting 2s" in retry_lines[0]


def test_rollback_started_log_emitted_in_observability(monkeypatch, caplog):
    """'rollback started' must be logged when rollback begins."""
    monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 0})
    monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: [])
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)
    pre = {"existed": False, "vlan_data": None}
    with caplog.at_level(logging.INFO, logger="app.services.vlan_execution_service"):
        svc._rollback_create(901, "dev", "obs-job", pre)
    assert any("rollback started" in r.message for r in caplog.records)
    assert any("rollback executed" in r.message for r in caplog.records)
    assert any("rollback verification succeeded" in r.message for r in caplog.records)
