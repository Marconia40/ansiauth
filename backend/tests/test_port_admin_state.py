"""Tests for the Step 2.2 port admin-state (shutdown / no shutdown) flow.

Covers:
* RBAC: unauthenticated, observer rejected, operator allowed
* unknown device → 404
* unsupported vendor → friendly 501 (no vendor leakage)
* end-to-end happy path (job reaches `completed`)
* idempotent no-op when admin state already matches
* rollback path restores the prior admin state
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.main import app
from app.models.device import Device
from app.models.port import PortInfo, PortListResponse
from app.services import job_service, port_execution_service, port_service


def _client(role: str) -> TestClient:
    token = create_access_token({"sub": role, "role": role})
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")


@pytest.fixture
def mock_device(monkeypatch):
    dev = Device(
        name="mock_device",
        host="192.0.2.1",
        vendor="huawei_vrp",
        username="admin",
        encrypted_password="encrypted",
        platform="vrp",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: dev if name == "mock_device" else None,
    )
    return dev


# ── RBAC + plumbing ──────────────────────────────────────────────────────────

def test_patch_rejects_unauthenticated():
    res = TestClient(app).patch(
        "/api/v1/ports/admin-state",
        json={"device": "mock_device", "interface": "Gi0/0/1", "enabled": False},
    )
    assert res.status_code == 401


def test_patch_rejects_observer(mock_mode, mock_device):
    res = _client("observer").patch(
        "/api/v1/ports/admin-state",
        json={"device": "mock_device", "interface": "Gi0/0/1", "enabled": False},
    )
    assert res.status_code == 403


def test_patch_operator_allowed(mock_mode, mock_device, monkeypatch):
    # Stub pre-state so the background task can drive to completion in mock mode.
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_admin",
        lambda interface, device: {"existed": True, "admin_up": True},
    )
    res = _client("operator").patch(
        "/api/v1/ports/admin-state",
        json={"device": "mock_device", "interface": "Gi0/0/1", "enabled": False},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body
    assert body["jobs"][0]["device"] == "mock_device"
    assert body["jobs"][0]["job_id"]


def test_patch_validates_interface_name(mock_mode, mock_device):
    res = _client("operator").patch(
        "/api/v1/ports/admin-state",
        json={"device": "mock_device", "interface": "", "enabled": False},
    )
    assert res.status_code in (400, 422)


def test_patch_unknown_device_returns_404(mock_mode):
    res = _client("operator").patch(
        "/api/v1/ports/admin-state",
        json={"device": "no-such-device", "interface": "Gi0/0/1", "enabled": False},
    )
    assert res.status_code == 404


def test_patch_unsupported_vendor_returns_friendly_501(monkeypatch):
    fake = Device(
        name="juniper-x",
        host="192.0.2.99",
        vendor="juniper",
        username="admin",
        encrypted_password="encrypted",
        platform="junos",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: fake if name == "juniper-x" else None,
    )
    res = _client("admin").patch(
        "/api/v1/ports/admin-state",
        json={"device": "juniper-x", "interface": "Gi0/0/1", "enabled": False},
    )
    assert res.status_code == 501
    body = res.json()
    assert body["error_code"] == "VENDOR_NOT_SUPPORTED"
    # Don't leak vendor / platform identifiers
    blob = res.text.lower()
    assert "juniper" not in blob
    assert "junos" not in blob


# ── End-to-end through the BackgroundTask runner ─────────────────────────────

def test_end_to_end_disable_completes_in_mock(monkeypatch, mock_mode, mock_device):
    """Disable on a currently-enabled port: job runs and lands at ``completed``."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_admin",
        lambda interface, device: {"existed": True, "admin_up": True},
    )

    res = _client("operator").patch(
        "/api/v1/ports/admin-state",
        json={"device": "mock_device", "interface": "Gi0/0/1", "enabled": False},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]

    job = job_service.get_job(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.retry_count == 0
    assert job.rollback_performed in (False, None)


def test_end_to_end_noop_when_already_in_state(monkeypatch, mock_mode, mock_device):
    """Asking to enable a port that's already enabled completes without
    calling the driver."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_admin",
        lambda interface, device: {"existed": True, "admin_up": True},
    )

    # Sentinel: confirm the service was NOT invoked.
    call_count = {"n": 0}
    original = port_service.set_port_admin_state_on_device

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(port_service, "set_port_admin_state_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/admin-state",
        json={"device": "mock_device", "interface": "Gi0/0/1", "enabled": True},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "completed"
    assert call_count["n"] == 0
    assert (job.result or {}).get("operation_result") == "noop"


def test_end_to_end_failure_triggers_rollback(monkeypatch, mock_mode, mock_device):
    """Force the driver call to fail; verify the runner restores the prior
    admin state via the rollback path and that the verification step
    confirms the restore."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_admin",
        lambda interface, device: {"existed": True, "admin_up": True},
    )

    calls: list[tuple[str, bool]] = []

    def _fake_set(interface, enabled, device):
        calls.append((interface, enabled))
        # Forward operation (enabled=False) fails; rollback (enabled=True) succeeds.
        if enabled is False:
            return {"rc": 1, "stdout": "", "stderr": "simulated failure", "success": False}
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    monkeypatch.setattr(port_service, "set_port_admin_state_on_device", _fake_set)
    # Verification step inside _rollback_admin_state re-reads list_ports.
    monkeypatch.setattr(
        port_service,
        "list_ports",
        lambda device: PortListResponse(
            device=device,
            vendor="huawei_vrp",
            ports=[PortInfo(name="Gi0/0/1", admin_up=True)],
        ),
    )
    monkeypatch.setattr("app.api.ports._RETRY_BASE_DELAY", 0.0)

    res = _client("operator").patch(
        "/api/v1/ports/admin-state",
        json={"device": "mock_device", "interface": "Gi0/0/1", "enabled": False},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]

    # Defensive yield — TestClient awaits the BackgroundTask but retries
    # may not be fully drained on slower CI machines.
    for _ in range(50):
        job = job_service.get_job(job_id)
        if job and job.status in {"failed", "completed"}:
            break
        time.sleep(0.05)

    job = job_service.get_job(job_id)
    assert job is not None
    assert job.status == "failed"
    assert job.rollback_performed is True
    assert job.rollback_success is True
    # Rollback re-issued the original admin state (True).
    assert ("Gi0/0/1", True) in calls


def test_end_to_end_unknown_interface_fails_cleanly(monkeypatch, mock_mode, mock_device):
    """Pre-state says the interface doesn't exist → job fails with a clean
    validation error and never calls the driver."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_admin",
        lambda interface, device: {"existed": False, "admin_up": None},
    )

    call_count = {"n": 0}
    original = port_service.set_port_admin_state_on_device

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(port_service, "set_port_admin_state_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/admin-state",
        json={"device": "mock_device", "interface": "Gi9/9/9", "enabled": False},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "failed"
    assert "does not exist" in (job.error or "").lower()
    assert call_count["n"] == 0
