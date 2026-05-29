"""Tests for the Step 2.3 port access-VLAN assignment flow.

Covers:
* RBAC: unauthenticated, observer rejected, operator allowed
* unknown device → 404
* unsupported vendor → friendly 501 (no vendor leakage)
* reserved VLAN IDs rejected at the API boundary (1002–1005)
* end-to-end happy path (job reaches `completed`)
* idempotent no-op when access VLAN already matches
* non-access mode port rejected cleanly
* rollback path restores the prior access VLAN
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
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "Gi0/0/1", "vlan_id": 10},
    )
    assert res.status_code == 401


def test_patch_rejects_observer(mock_mode, mock_device):
    res = _client("observer").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "Gi0/0/1", "vlan_id": 10},
    )
    assert res.status_code == 403


def test_patch_operator_allowed(mock_mode, mock_device, monkeypatch):
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_access_vlan",
        lambda interface, device: {"existed": True, "mode": "access", "access_vlan": 1},
    )
    res = _client("operator").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "Gi0/0/1", "vlan_id": 10},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body
    assert body["jobs"][0]["device"] == "mock_device"
    assert body["jobs"][0]["job_id"]


def test_patch_validates_interface_name(mock_mode, mock_device):
    res = _client("operator").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "", "vlan_id": 10},
    )
    assert res.status_code in (400, 422)


def test_patch_unknown_device_returns_404(mock_mode):
    res = _client("operator").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "no-such-device", "interface": "Gi0/0/1", "vlan_id": 10},
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
        "/api/v1/ports/access-vlan",
        json={"device": "juniper-x", "interface": "Gi0/0/1", "vlan_id": 10},
    )
    assert res.status_code == 501
    body = res.json()
    assert body["error_code"] == "VENDOR_NOT_SUPPORTED"
    blob = res.text.lower()
    assert "juniper" not in blob
    assert "junos" not in blob


# ── VLAN ID validation ────────────────────────────────────────────────────────

@pytest.mark.parametrize("vlan_id", [1002, 1003, 1004, 1005])
def test_reserved_vlan_ids_rejected(mock_mode, mock_device, vlan_id):
    res = _client("operator").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "Gi0/0/1", "vlan_id": vlan_id},
    )
    assert res.status_code == 400


def test_vlan_id_out_of_range_rejected(mock_mode, mock_device):
    res = _client("operator").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "Gi0/0/1", "vlan_id": 4095},
    )
    assert res.status_code in (400, 422)


def test_vlan_id_zero_rejected(mock_mode, mock_device):
    res = _client("operator").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "Gi0/0/1", "vlan_id": 0},
    )
    assert res.status_code in (400, 422)


# ── End-to-end through the BackgroundTask runner ─────────────────────────────

def test_end_to_end_set_vlan_completes_in_mock(monkeypatch, mock_mode, mock_device):
    """Set VLAN 20 on a port currently in VLAN 1: job runs and lands at ``completed``."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_access_vlan",
        lambda interface, device: {"existed": True, "mode": "access", "access_vlan": 1},
    )

    res = _client("operator").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "Gi0/0/1", "vlan_id": 20},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]

    job = job_service.get_job(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.retry_count == 0
    assert job.rollback_performed in (False, None)


def test_end_to_end_noop_when_vlan_already_matches(monkeypatch, mock_mode, mock_device):
    """Requesting the same VLAN that's already active completes without calling the driver."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_access_vlan",
        lambda interface, device: {"existed": True, "mode": "access", "access_vlan": 10},
    )

    call_count = {"n": 0}
    original = port_service.set_port_access_vlan_on_device

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(port_service, "set_port_access_vlan_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "Gi0/0/1", "vlan_id": 10},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "completed"
    assert call_count["n"] == 0
    assert (job.result or {}).get("operation_result") == "noop"


def test_end_to_end_trunk_port_sets_pvid(monkeypatch, mock_mode, mock_device):
    """A port in trunk mode should set the trunk PVID via set_trunk_pvid_vlan_on_device."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_access_vlan",
        lambda interface, device: {"existed": True, "mode": "trunk", "access_vlan": 1},
    )

    pvid_calls = {"n": 0}
    access_calls = {"n": 0}
    original_pvid = port_service.set_trunk_pvid_vlan_on_device

    def _spy_pvid(*args, **kwargs):
        pvid_calls["n"] += 1
        return original_pvid(*args, **kwargs)

    def _spy_access(*args, **kwargs):
        access_calls["n"] += 1
        return port_service.set_port_access_vlan_on_device(*args, **kwargs)

    monkeypatch.setattr(port_service, "set_trunk_pvid_vlan_on_device", _spy_pvid)
    monkeypatch.setattr(port_service, "set_port_access_vlan_on_device", _spy_access)

    res = _client("operator").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "Gi0/0/1", "vlan_id": 10},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "completed"
    assert pvid_calls["n"] == 1
    assert access_calls["n"] == 0


def test_end_to_end_failure_triggers_rollback(monkeypatch, mock_mode, mock_device):
    """Force the driver call to fail; verify the runner restores the prior
    access VLAN via the rollback path."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_access_vlan",
        lambda interface, device: {"existed": True, "mode": "access", "access_vlan": 5},
    )

    calls: list[tuple[str, int]] = []

    def _fake_set(interface, vlan_id, device):
        calls.append((interface, vlan_id))
        if vlan_id == 20:
            return {"rc": 1, "stdout": "", "stderr": "simulated failure", "success": False}
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    monkeypatch.setattr(port_service, "set_port_access_vlan_on_device", _fake_set)
    monkeypatch.setattr(
        port_service,
        "list_ports",
        lambda device: PortListResponse(
            device=device,
            vendor="huawei_vrp",
            ports=[PortInfo(name="Gi0/0/1", mode="access", access_vlan=5)],
        ),
    )
    monkeypatch.setattr("app.api.ports._RETRY_BASE_DELAY", 0.0)

    res = _client("operator").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "Gi0/0/1", "vlan_id": 20},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]

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
    # Rollback restored the original VLAN (5).
    assert ("Gi0/0/1", 5) in calls


def test_end_to_end_unknown_interface_fails_cleanly(monkeypatch, mock_mode, mock_device):
    """Pre-state says the interface doesn't exist → job fails cleanly."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_access_vlan",
        lambda interface, device: {"existed": False, "mode": None, "access_vlan": None},
    )

    call_count = {"n": 0}
    original = port_service.set_port_access_vlan_on_device

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(port_service, "set_port_access_vlan_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/access-vlan",
        json={"device": "mock_device", "interface": "Gi9/9/9", "vlan_id": 10},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "failed"
    assert "does not exist" in (job.error or "").lower()
    assert call_count["n"] == 0
