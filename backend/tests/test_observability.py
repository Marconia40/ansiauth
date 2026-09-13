"""Tests for observability: execution_summary structure, current_step
lifecycle, and group-job aggregation.

Modernized: ``audit_service``/``vlan_service`` are gone;
``app.services.vlan_execution_service`` is fully deleted (retry/rollback now
live on ``Orquestador``); ``group_job_service``/``GroupJob``/``DeviceExecution``
are fully deleted (no DB row for a group job anymore -- ``GET
/group-jobs/{id}`` is backed by ``JobRepository.resumen_de_grupo()``, computed
on the fly from real per-device ``Job`` rows sharing a ``group_job_id``).

The 3 "rollback started"/"rollback executed"/"rollback verification
succeeded"/"rollback skipped" log-line tests have no modern equivalent --
``VLAN.ejecutar_rollback()`` (app/models/vlan.py) and
``Orquestador._rollback()`` do the same job with zero logging calls anywhere
in either method (confirmed by grep) -- deleted rather than pointed at
nonexistent log text.
"""
import logging

import app.services.orquestador as orquestador_module
from app.composition import (
    audit_repository,
    device_repository,
    device_sync_service,
    inventory,
    job_repository,
    plugin_registry,
    site_repository,
)
from app.core.exceptions import ValidationError as _ValidationError
from app.models.job import Job
from app.models.visibility_scope import VisibilityScope
from app.services.vendors.mock import MockVendor

plugin_registry.registrar("cisco_ios", MockVendor())
plugin_registry.registrar("huawei_vrp", MockVendor())

_SITE_NAME = "VLAN Orchestration Test Site"
_ADMIN_SCOPE = VisibilityScope(es_system_admin=True, grants=())


def _ensure_site(name: str = _SITE_NAME):
    try:
        return site_repository.crear_con_grupo_default(name, kind="REGULAR")
    except ValueError:
        return site_repository.list(name=name)[0]


def _ensure_device(name: str, vendor: str = "cisco_ios", host: "str | None" = None):
    existing = device_repository.get(name)
    if existing is not None:
        return existing
    site = _ensure_site()
    try:
        device = inventory.register(
            name=name, host=host or f"10.90.0.{abs(hash(name)) % 250 + 1}",
            vendor=vendor, platform="ios",
            username="admin", password="admin123",
            site_id=site.id, device_group_id=None,
            actor={"username": "test-setup"},
        )
    except _ValidationError:
        return device_repository.get(name)
    try:
        device_sync_service.sync_vlans(device)
    except Exception:
        pass
    return device


_ensure_device("mock_device")


# ── execution_summary structure ───────────────────────────────────────────────

def test_execution_summary_present_on_success(client):
    """execution_summary must appear in the job response for completed jobs."""
    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 700, "name": "OBS_VLAN", "devices": ["mock_device"]},
    )
    assert resp.status_code == 202
    job_id = resp.json()["data"]["jobs"][0]["job_id"]

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
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda s: None)

    call_number = {"n": 0}

    def _transient_then_succeed(self, vlan_id, name, device, password):
        call_number["n"] += 1
        if call_number["n"] <= 2:
            return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(MockVendor, "create_vlan", _transient_then_succeed)

    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 701, "name": "RETRY_OBS", "devices": ["mock_device"]},
    )
    assert resp.status_code == 202
    job_id = resp.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    assert job["retry_count"] == 2
    summary = job["execution_summary"]
    assert summary["attempts"] == 3
    assert summary["duration_ms"] is not None


def test_execution_summary_on_failed_with_rollback(client, monkeypatch):
    """execution_summary must reflect rollback state on failed jobs."""
    monkeypatch.setattr(
        MockVendor, "create_vlan",
        lambda self, vlan_id, name, device, password: {"rc": 1, "stdout": "", "stderr": "configuration syntax error"},
    )
    monkeypatch.setattr(
        MockVendor, "delete_vlan",
        lambda self, vlan_id, device, password: {"rc": 0, "stdout": "deleted", "stderr": ""},
    )

    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 901, "name": "ROLLBACK_OBS", "devices": ["mock_device"]},
    )
    assert resp.status_code == 202
    job_id = resp.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    summary = job["execution_summary"]
    assert summary["rollback_performed"] is True
    assert summary["rollback_success"] is True
    assert summary["attempts"] == 1


def test_execution_summary_attempts_none_for_pending_job(client):
    """execution_summary.attempts must be None for jobs not yet finished."""
    job = Job(operation="vlan", device="mock_device", parameters={})
    job_repository.add(job)

    resp = client.get(f"/api/v1/jobs/{job.job_id}").json()["data"]
    assert resp["status"] == "pending"
    assert resp["execution_summary"]["attempts"] is None


# ── current_step lifecycle ────────────────────────────────────────────────────

def test_current_step_rollback_completed_after_failure(client, monkeypatch):
    """current_step must be 'failed' after a failed job with rollback --
    Orquestador doesn't set a distinct 'rollback_completed' step (the old
    vlan_execution_service.py did); Job.marcar_fallido() always sets
    current_step="failed" regardless of whether a rollback ran."""
    monkeypatch.setattr(
        MockVendor, "create_vlan",
        lambda self, vlan_id, name, device, password: {"rc": 1, "stdout": "", "stderr": "configuration syntax error"},
    )
    monkeypatch.setattr(
        MockVendor, "delete_vlan",
        lambda self, vlan_id, device, password: {"rc": 0, "stdout": "deleted", "stderr": ""},
    )

    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 902, "name": "STEP_OBS", "devices": ["mock_device"]},
    )
    assert resp.status_code == 202
    job_id = resp.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["rollback_performed"] is True
    assert job["current_step"] == "failed"


def test_current_step_completed_on_success(client):
    """current_step must be 'completed' after a successful create."""
    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 703, "name": "STEP_OK", "devices": ["mock_device"]},
    )
    assert resp.status_code == 202
    job_id = resp.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    assert job["current_step"] == "completed"


def test_current_step_is_executing_immediately_after_marcar_iniciado():
    """current_step must be 'executing' immediately after a job transitions
    to running. Ported as a direct unit check on Job.marcar_iniciado()
    (app/models/job.py) -- the original test spied on job_service.update_job
    from a background thread mid-flight; under CELERY_TASK_ALWAYS_EAGER every
    dispatch runs to full completion synchronously before the HTTP response
    returns, so there's no longer a window to observe an in-flight job via a
    second request. The transition itself (running -> current_step=
    "executing") is the thing being tested, and is exercised for real on
    every job the other tests in this file create."""
    job = Job()
    job.marcar_iniciado()
    assert job.status == "running"
    assert job.current_step == "executing"


# ── Retry wait duration + device name in logs ─────────────────────────────────

def test_retry_wait_duration_in_log(monkeypatch, caplog):
    """Retry log must include 'delay=Xs' with the computed delay."""
    from app.composition import orquestador

    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: None)

    def _transient():
        return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}

    with caplog.at_level(logging.INFO, logger="app.services.orquestador"):
        orquestador._ejecutar_con_retry(_transient, Job(), "timing-test-device", max_retries=1, retry_base_delay=2.0)

    retry_lines = [r.message for r in caplog.records if "reintentando" in r.message]
    assert len(retry_lines) == 1
    assert "delay=2.00s" in retry_lines[0]


def test_retry_log_includes_device_name(monkeypatch, caplog):
    """Retry log must include the device name -- _ejecutar_con_retry()'s
    `device` parameter is a plain string used only for logging, so no real
    Device row is needed to exercise this."""
    from app.composition import orquestador

    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: None)

    def _transient():
        return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}

    with caplog.at_level(logging.INFO, logger="app.services.orquestador"):
        orquestador._ejecutar_con_retry(_transient, Job(), "switch1", max_retries=1, retry_base_delay=0.01)

    retry_lines = [r.message for r in caplog.records if "reintentando" in r.message]
    assert len(retry_lines) == 1
    assert "switch1" in retry_lines[0]


# ── Group-job aggregation ──────────────────────────────────────────────────────

def test_group_job_summary_partial_success_false_when_all_succeed():
    """execution_summary.partial_success must be False when all devices succeed."""
    import uuid
    group_job_id = str(uuid.uuid4())
    for device in ("sw1", "sw2"):
        j = Job(operation="vlan", device=device, group_job_id=group_job_id)
        j.marcar_iniciado()
        j.marcar_completado({"rc": 0})
        job_repository.add(j)

    resumen = job_repository.resumen_de_grupo(group_job_id)
    assert resumen["execution_summary"]["partial_success"] is False


def test_group_job_summary_partial_success_true_on_mixed():
    """execution_summary.partial_success must be True when some devices
    succeed and some fail."""
    import uuid
    group_job_id = str(uuid.uuid4())
    j1 = Job(operation="vlan", device="sw1", group_job_id=group_job_id)
    j1.marcar_iniciado()
    j1.marcar_completado({"rc": 0})
    job_repository.add(j1)

    j2 = Job(operation="vlan", device="sw2", group_job_id=group_job_id)
    j2.marcar_iniciado()
    j2.marcar_fallido("boom")
    job_repository.add(j2)

    resumen = job_repository.resumen_de_grupo(group_job_id)
    assert resumen["execution_summary"]["partial_success"] is True


def test_group_job_summary_duration_ms_set_after_completion():
    """execution_summary.duration_ms must be a non-negative integer after
    the group job finishes."""
    import uuid
    group_job_id = str(uuid.uuid4())
    j = Job(operation="vlan", device="sw1", group_job_id=group_job_id)
    j.marcar_iniciado()
    j.marcar_completado({"rc": 0})
    job_repository.add(j)

    resumen = job_repository.resumen_de_grupo(group_job_id)
    s = resumen["execution_summary"]
    assert s["duration_ms"] is not None
    assert isinstance(s["duration_ms"], int)
    assert s["duration_ms"] >= 0


def test_group_job_summary_duration_ms_none_while_pending():
    """execution_summary.duration_ms must be None when the group job has
    not yet started (no job in the group has a started_at yet)."""
    import uuid
    group_job_id = str(uuid.uuid4())
    j = Job(operation="vlan", device="sw1", group_job_id=group_job_id)
    job_repository.add(j)

    resumen = job_repository.resumen_de_grupo(group_job_id)
    assert resumen["execution_summary"]["duration_ms"] is None


def test_api_group_job_summary_includes_partial_success_and_duration(client):
    """GET /group-jobs/{id} must return partial_success and duration_ms in
    execution_summary."""
    payload = {"vlan_id": 803, "name": "OBSGRP", "devices": ["mock_device"]}
    create_resp = client.post("/api/v1/vlans/", json=payload)
    assert create_resp.status_code == 202
    group_job_id = create_resp.json()["data"]["group_job_id"]

    resp = client.get(f"/api/v1/group-jobs/{group_job_id}")
    assert resp.status_code == 200
    s = resp.json()["data"]["execution_summary"]
    assert "partial_success" in s
    assert "duration_ms" in s
    assert isinstance(s["partial_success"], bool)


def test_api_per_device_result_fields_complete(client):
    """device_results in group job API response must include all required fields."""
    payload = {"vlan_id": 820, "name": "DEVFLDS", "devices": ["mock_device"]}
    create_resp = client.post("/api/v1/vlans/", json=payload)
    assert create_resp.status_code == 202
    group_job_id = create_resp.json()["data"]["group_job_id"]

    resp = client.get(f"/api/v1/group-jobs/{group_job_id}")
    assert resp.status_code == 200
    dr = resp.json()["data"]["device_results"][0]
    for field in ("device", "status", "retry_count", "rollback_performed", "rollback_success", "duration_ms"):
        assert field in dr, f"Missing field: {field}"
