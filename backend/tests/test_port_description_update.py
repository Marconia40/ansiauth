"""Tests for the Step 2.1 port-description update flow.

Covers:
* validator rejects malformed input
* PATCH endpoint returns the standard job envelope
* RBAC: observer rejected, operator allowed
* unsupported vendor returns the friendly 501
* mock-mode end-to-end (background task runs, job completes)
* rollback path verifies prior state is restored
* idempotent no-op when description already matches
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
from app.validators import port_validator


def _client(role: str) -> TestClient:
    token = create_access_token({"sub": role, "role": role})
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


# ── Validators ────────────────────────────────────────────────────────────────

def test_validator_accepts_typical_interface_names():
    for name in [
        "Gi0/0/1",
        "GigabitEthernet0/0/1",
        "GigabitEthernet1/0/1",
        "Te1/1",
        "Po1",
        "FastEthernet0/24",
    ]:
        port_validator.validate_interface_name(name)


def test_validator_rejects_bad_interface_names():
    for name in ["", " ", "1/0/1", "GigaBitEthernet 0/0/1", "Gi0\n0/1", "x"]:
        with pytest.raises(ValueError):
            port_validator.validate_interface_name(name)


def test_validator_accepts_empty_description():
    # Empty description means "clear" — must NOT raise.
    port_validator.validate_description("")


def test_validator_rejects_control_chars_in_description():
    with pytest.raises(ValueError):
        port_validator.validate_description("with\nnewline")
    with pytest.raises(ValueError):
        port_validator.validate_description("with\ttab")  # \t is x09 control char


def test_validator_rejects_overlength_description():
    with pytest.raises(ValueError):
        port_validator.validate_description("x" * 201)


# ── PATCH /api/v1/ports/description — RBAC + happy path ──────────────────────

@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")


@pytest.fixture
def mock_device(monkeypatch):
    """Wire a fake device named 'mock_device' through the dispatcher path so
    the PATCH endpoint reaches `enqueue_update_description_job`."""
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


def test_patch_rejects_unauthenticated():
    anon = TestClient(app)
    res = anon.patch(
        "/api/v1/ports/description",
        json={"device": "mock_device", "interface": "Gi0/0/1", "description": "x"},
    )
    assert res.status_code == 401


def test_patch_rejects_observer(mock_mode, mock_device):
    res = _client("observer").patch(
        "/api/v1/ports/description",
        json={"device": "mock_device", "interface": "Gi0/0/1", "description": "x"},
    )
    assert res.status_code == 403


def test_patch_operator_allowed(mock_mode, mock_device):
    res = _client("operator").patch(
        "/api/v1/ports/description",
        json={"device": "mock_device", "interface": "Gi0/0/1", "description": "user-desk"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body
    assert body["jobs"][0]["device"] == "mock_device"
    assert body["jobs"][0]["job_id"]
    # job is queued — we don't assert completion here (background task runs
    # asynchronously); see test_end_to_end_completes_in_mock below.


def test_patch_validates_interface_name(mock_mode, mock_device):
    res = _client("operator").patch(
        "/api/v1/ports/description",
        json={"device": "mock_device", "interface": "", "description": "x"},
    )
    # Pydantic rejects min_length first → 422
    assert res.status_code in (400, 422)


def test_patch_validates_description_length(mock_mode, mock_device):
    res = _client("operator").patch(
        "/api/v1/ports/description",
        json={
            "device": "mock_device",
            "interface": "Gi0/0/1",
            "description": "x" * 1000,
        },
    )
    assert res.status_code in (400, 422)


def test_patch_unknown_device_returns_404(mock_mode):
    res = _client("operator").patch(
        "/api/v1/ports/description",
        json={"device": "no-such-device", "interface": "Gi0/0/1", "description": "x"},
    )
    assert res.status_code == 404


def test_patch_unsupported_vendor_returns_friendly_501(monkeypatch):
    """A device whose vendor has no port driver registered returns 501 with
    the operator-friendly message — vendor / platform never leak."""
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
    # Admin bypasses site-scoped RBAC; we want to test the vendor gating
    # rather than the device-allowlist gating.
    res = _client("admin").patch(
        "/api/v1/ports/description",
        json={"device": "juniper-x", "interface": "Gi0/0/1", "description": "x"},
    )
    assert res.status_code == 501
    body = res.json()
    assert body["error_code"] == "VENDOR_NOT_SUPPORTED"
    blob = res.text.lower()
    assert "juniper" not in blob
    assert "junos" not in blob


# ── End-to-end through the BackgroundTask runner ─────────────────────────────

def test_end_to_end_completes_in_mock(monkeypatch, mock_mode, mock_device):
    """Mock-mode runner should drive the job to ``completed`` and emit
    an audit event."""
    # Stub pre-state so we don't depend on a vendor driver in mock mode.
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_description",
        lambda interface, device: {"existed": True, "description": "old-desc"},
    )

    res = _client("operator").patch(
        "/api/v1/ports/description",
        json={"device": "mock_device", "interface": "Gi0/0/1", "description": "new-desc"},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]

    # BackgroundTasks runs after the response; the TestClient returns once
    # the task has executed.  We can therefore inspect the job state immediately.
    job = job_service.get_job(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.retry_count == 0
    assert job.rollback_performed in (False, None)


def test_end_to_end_noop_when_description_unchanged(monkeypatch, mock_mode, mock_device):
    """If the pre-state description matches the requested value, the job
    completes as a no-op without invoking the driver."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_description",
        lambda interface, device: {"existed": True, "description": "same"},
    )

    # Sentinel: if the service actually drives the playbook the call count
    # would be 1; expect 0.
    call_count = {"n": 0}
    original = port_service.update_port_description_on_device

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(port_service, "update_port_description_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/description",
        json={"device": "mock_device", "interface": "Gi0/0/1", "description": "same"},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]

    job = job_service.get_job(job_id)
    assert job.status == "completed"
    assert call_count["n"] == 0
    res_dict = job.result or {}
    assert res_dict.get("operation_result") == "noop"


def test_end_to_end_failure_triggers_rollback(monkeypatch, mock_mode, mock_device):
    """Force the driver call to fail; verify the runner triggers rollback
    using the captured pre-state value."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_description",
        lambda interface, device: {"existed": True, "description": "previous-desc"},
    )

    # Track the rollback restoration call.
    calls: list[tuple[str, str]] = []

    def _fake_update(interface, description, device):
        calls.append((description, device))
        if description == "new-desc":
            return {"rc": 1, "stdout": "", "stderr": "simulated failure", "success": False}
        # Rollback call (description == "previous-desc") succeeds.
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    monkeypatch.setattr(
        port_service, "update_port_description_on_device", _fake_update,
    )
    # Verification step in rollback re-reads list_ports — stub to return the
    # restored description.
    monkeypatch.setattr(
        port_service,
        "list_ports",
        lambda device: PortListResponse(
            device=device,
            vendor="huawei_vrp",
            ports=[PortInfo(name="Gi0/0/1", description="previous-desc")],
        ),
    )
    # Speed retries down so the test doesn't sleep forever.
    monkeypatch.setattr("app.api.ports._RETRY_BASE_DELAY", 0.0)

    res = _client("operator").patch(
        "/api/v1/ports/description",
        json={"device": "mock_device", "interface": "Gi0/0/1", "description": "new-desc"},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]

    # Wait briefly for the BackgroundTask to drain (TestClient awaits it,
    # but the retry sleeps are zero so this is just a defensive yield).
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
    # Rollback re-issued the original description
    assert ("previous-desc", "mock_device") in calls
