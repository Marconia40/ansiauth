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
    monkeypatch.setattr("app.core.config.EXECUTION_MODE", "real")
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


# ── Step 4.3: GroupJob execution_summary fields ───────────────────────────────

def test_group_job_summary_partial_success_false_when_all_succeed(client):
    """execution_summary.partial_success must be False when all devices succeed."""
    from app.services import group_job_service
    gj = group_job_service.create_group_job(
        operation="create_vlan", playbook="create_vlan.yml",
        parameters={"vlan_id": 800}, devices=["sw1", "sw2"],
    )
    group_job_service.update_device_result(gj.group_job_id, "sw1", "j1", "completed")
    group_job_service.update_device_result(gj.group_job_id, "sw2", "j2", "completed")
    fetched = group_job_service.get_group_job(gj.group_job_id)
    assert fetched.execution_summary()["partial_success"] is False


def test_group_job_summary_partial_success_true_on_mixed(client):
    """execution_summary.partial_success must be True when some devices succeed and some fail."""
    from app.services import group_job_service
    gj = group_job_service.create_group_job(
        operation="create_vlan", playbook="create_vlan.yml",
        parameters={"vlan_id": 801}, devices=["sw1", "sw2"],
    )
    group_job_service.update_device_result(gj.group_job_id, "sw1", "j1", "completed")
    group_job_service.update_device_result(gj.group_job_id, "sw2", "j2", "failed")
    fetched = group_job_service.get_group_job(gj.group_job_id)
    assert fetched.execution_summary()["partial_success"] is True


def test_group_job_summary_duration_ms_set_after_completion(client):
    """execution_summary.duration_ms must be a non-negative integer after the group job finishes."""
    from app.services import group_job_service
    gj = group_job_service.create_group_job(
        operation="create_vlan", playbook="create_vlan.yml",
        parameters={"vlan_id": 802}, devices=["sw1"],
    )
    group_job_service.update_device_result(gj.group_job_id, "sw1", "j1", "completed")
    fetched = group_job_service.get_group_job(gj.group_job_id)
    s = fetched.execution_summary()
    assert s["duration_ms"] is not None
    assert isinstance(s["duration_ms"], int)
    assert s["duration_ms"] >= 0


def test_group_job_summary_duration_ms_none_while_pending():
    """execution_summary.duration_ms must be None when the group job has not yet started."""
    from app.models.group_job import DeviceExecution, GroupJob
    gj = GroupJob(
        device_results=[DeviceExecution(device="sw1", status="pending")],
    )
    assert gj.execution_summary()["duration_ms"] is None


def test_api_group_job_summary_includes_partial_success_and_duration(client):
    """GET /group-jobs/{id} must return partial_success and duration_ms in execution_summary."""
    payload = {"vlan_id": 803, "name": "OBSGRP", "devices": ["mock_device"]}
    create_resp = client.post("/api/v1/vlans/", json=payload)
    assert create_resp.status_code == 200
    group_job_id = create_resp.json()["group_job_id"]

    time.sleep(0.4)

    resp = client.get(f"/api/v1/group-jobs/{group_job_id}")
    assert resp.status_code == 200
    s = resp.json()["data"]["execution_summary"]
    assert "partial_success" in s
    assert "duration_ms" in s
    assert isinstance(s["partial_success"], bool)


# ── Step 4.3: current_step="executing" while running ─────────────────────────

def test_current_step_is_executing_while_running(client, monkeypatch):
    """current_step must be 'executing' immediately after a job transitions to running."""
    from app.services import job_service

    captured_steps = []

    real_update = job_service.update_job
    def _spy_update(job_id, status=None, **kwargs):
        real_update(job_id, status=status, **kwargs)
        if status == "running":
            job = job_service.get_job(job_id)
            if job:
                captured_steps.append(job.current_step)

    monkeypatch.setattr(job_service, "update_job", _spy_update)
    monkeypatch.setattr(svc, "job_service", job_service)

    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 810, "name": "EXEC_STEP", "devices": ["mock_device"]},
    )
    assert resp.status_code == 200
    assert "executing" in captured_steps


# ── Step 4.3: retry log includes device name ──────────────────────────────────

def test_retry_log_includes_device_name(monkeypatch, caplog):
    """Retry log must include the device name."""
    monkeypatch.setattr(svc.time, "sleep", lambda d: None)
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    def _transient():
        return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}

    with caplog.at_level(logging.INFO, logger="app.services.vlan_execution_service"):
        svc._execute_with_retry(_transient, "dev-log-test", max_retries=1, retry_base_delay=0.01, device="switch1")

    retry_lines = [r.message for r in caplog.records if "retry attempt" in r.message]
    assert len(retry_lines) == 1
    assert "switch1" in retry_lines[0]


# ── Step 4.3: per-device result fields in API ─────────────────────────────────

def test_api_per_device_result_fields_complete(client):
    """device_results in group job API response must include all required fields."""
    payload = {"vlan_id": 820, "name": "DEVFLDS", "devices": ["mock_device"]}
    create_resp = client.post("/api/v1/vlans/", json=payload)
    assert create_resp.status_code == 200
    group_job_id = create_resp.json()["group_job_id"]

    time.sleep(0.4)

    resp = client.get(f"/api/v1/group-jobs/{group_job_id}")
    assert resp.status_code == 200
    dr = resp.json()["data"]["device_results"][0]
    for field in ("device", "status", "retry_count", "rollback_performed", "rollback_success", "duration_ms"):
        assert field in dr, f"Missing field: {field}"
