"""Tests for the Step 2.4 trunk allowed-VLAN management flow.

Covers:
* RBAC: unauthenticated, observer rejected, operator allowed
* unknown device → 404
* unsupported vendor → friendly 501 (no vendor leakage)
* invalid VLAN IDs rejected at the API boundary (reserved 1002-1005, out-of-range)
* empty vlans list rejected (Pydantic)
* end-to-end replace/add/remove happy paths
* idempotent no-op when desired list already matches current
* access-mode port rejected cleanly (mode guard)
* unknown-current-list blocks add/remove; passes replace
* remove would empty trunk → fail cleanly
* rollback path restores prior VLAN list on failure
* VLAN list compression utilities (cisco + huawei formats)
* _compute_desired_vlans logic
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
from app.services.port_execution_service import _compute_desired_vlans
from app.validators.port_validator import compress_vlans_cisco, compress_vlans_huawei


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


# ── Compression utility unit tests ───────────────────────────────────────────

def test_compress_vlans_cisco_single():
    assert compress_vlans_cisco([10]) == "10"


def test_compress_vlans_cisco_range():
    assert compress_vlans_cisco([10, 11, 12]) == "10-12"


def test_compress_vlans_cisco_mixed():
    assert compress_vlans_cisco([10, 11, 12, 20, 30, 31]) == "10-12,20,30-31"


def test_compress_vlans_cisco_unordered_dedup():
    assert compress_vlans_cisco([30, 10, 11, 30, 12]) == "10-12,30"


def test_compress_vlans_huawei_single():
    assert compress_vlans_huawei([10]) == "10"


def test_compress_vlans_huawei_range():
    assert compress_vlans_huawei([10, 11, 12]) == "10 to 12"


def test_compress_vlans_huawei_mixed():
    assert compress_vlans_huawei([10, 11, 12, 20, 30, 31]) == "10 to 12 20 30 to 31"


# ── _compute_desired_vlans unit tests ────────────────────────────────────────

def test_compute_replace():
    assert _compute_desired_vlans("replace", [1, 10], [20, 30]) == [20, 30]


def test_compute_replace_no_current_needed():
    assert _compute_desired_vlans("replace", None, [10, 20]) == [10, 20]


def test_compute_add():
    assert _compute_desired_vlans("add", [1, 10], [10, 20]) == [1, 10, 20]


def test_compute_add_returns_none_when_current_unknown():
    assert _compute_desired_vlans("add", None, [10]) is None


def test_compute_remove():
    assert _compute_desired_vlans("remove", [1, 10, 20], [10]) == [1, 20]


def test_compute_remove_returns_none_when_current_unknown():
    assert _compute_desired_vlans("remove", None, [10]) is None


def test_compute_remove_to_empty():
    assert _compute_desired_vlans("remove", [10], [10]) == []


# ── RBAC + plumbing ──────────────────────────────────────────────────────────

def test_patch_rejects_unauthenticated():
    res = TestClient(app).patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [10, 20]},
    )
    assert res.status_code == 401


def test_patch_rejects_observer(mock_mode, mock_device):
    res = _client("observer").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [10, 20]},
    )
    assert res.status_code == 403


def test_patch_operator_allowed(mock_mode, mock_device, monkeypatch):
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_trunk_vlans",
        lambda interface, device: {"existed": True, "mode": "trunk", "allowed_vlans": [1, 10]},
    )
    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [20, 30]},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body
    assert body["jobs"][0]["device"] == "mock_device"
    assert body["jobs"][0]["job_id"]


def test_patch_unknown_device_returns_404(mock_mode):
    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "no-such-device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [10]},
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
        "/api/v1/ports/trunk-vlans",
        json={"device": "juniper-x", "interface": "Gi0/0/1", "mode": "replace", "vlans": [10]},
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
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [vlan_id]},
    )
    assert res.status_code == 400


def test_vlan_id_out_of_range_rejected(mock_mode, mock_device):
    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [4095]},
    )
    assert res.status_code in (400, 422)


def test_empty_vlans_list_rejected(mock_mode, mock_device):
    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": []},
    )
    assert res.status_code in (400, 422)


def test_duplicate_vlans_accepted_and_deduplicated(mock_mode, mock_device):
    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [10, 10]},
    )
    assert res.status_code == 200


# ── End-to-end runner scenarios ───────────────────────────────────────────────

def test_e2e_replace_completes(monkeypatch, mock_mode, mock_device):
    """Replace mode: job completes and driver is called."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_trunk_vlans",
        lambda interface, device: {"existed": True, "mode": "trunk", "allowed_vlans": [1, 10]},
    )
    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [20, 30]},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.retry_count == 0
    assert job.rollback_performed in (False, None)


def test_e2e_add_merges_with_current(monkeypatch, mock_mode, mock_device):
    """Add mode: desired = current ∪ requested."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_trunk_vlans",
        lambda interface, device: {"existed": True, "mode": "trunk", "allowed_vlans": [1, 10]},
    )
    calls: list[list[int]] = []
    original = port_service.set_trunk_allowed_vlans_on_device

    def _spy(interface, vlan_list, device):
        calls.append(sorted(vlan_list))
        return original(interface, vlan_list, device)
    monkeypatch.setattr(port_service, "set_trunk_allowed_vlans_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "add", "vlans": [20]},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "completed"
    # Driver called with union of [1, 10] and [20]
    assert calls == [[1, 10, 20]]


def test_e2e_remove_subtracts_from_current(monkeypatch, mock_mode, mock_device):
    """Remove mode: desired = current - requested."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_trunk_vlans",
        lambda interface, device: {"existed": True, "mode": "trunk", "allowed_vlans": [1, 10, 20]},
    )
    calls: list[list[int]] = []
    original = port_service.set_trunk_allowed_vlans_on_device

    def _spy(interface, vlan_list, device):
        calls.append(sorted(vlan_list))
        return original(interface, vlan_list, device)
    monkeypatch.setattr(port_service, "set_trunk_allowed_vlans_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "remove", "vlans": [10]},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "completed"
    assert calls == [[1, 20]]


def test_e2e_noop_when_replace_already_matches(monkeypatch, mock_mode, mock_device):
    """Replace with the same list → no-op, driver not called."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_trunk_vlans",
        lambda interface, device: {"existed": True, "mode": "trunk", "allowed_vlans": [10, 20]},
    )
    call_count = {"n": 0}
    original = port_service.set_trunk_allowed_vlans_on_device

    def _spy(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(port_service, "set_trunk_allowed_vlans_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [10, 20]},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "completed"
    assert call_count["n"] == 0
    assert (job.result or {}).get("operation_result") == "noop"


def test_e2e_access_port_fails_cleanly(monkeypatch, mock_mode, mock_device):
    """A port in access mode must be rejected without calling the driver."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_trunk_vlans",
        lambda interface, device: {"existed": True, "mode": "access", "allowed_vlans": None},
    )
    call_count = {"n": 0}

    def _spy(*args, **kwargs):
        call_count["n"] += 1
    monkeypatch.setattr(port_service, "set_trunk_allowed_vlans_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [10]},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "failed"
    assert "trunk" in (job.error or "").lower()
    assert call_count["n"] == 0


def test_e2e_add_fails_when_current_vlans_unknown(monkeypatch, mock_mode, mock_device):
    """Add mode with unknown current list → fail cleanly, driver not called."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_trunk_vlans",
        lambda interface, device: {"existed": True, "mode": "trunk", "allowed_vlans": None},
    )
    call_count = {"n": 0}

    def _spy(*args, **kwargs):
        call_count["n"] += 1
    monkeypatch.setattr(port_service, "set_trunk_allowed_vlans_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "add", "vlans": [10]},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "failed"
    assert "unknown" in (job.error or "").lower()
    assert call_count["n"] == 0


def test_e2e_replace_succeeds_when_current_vlans_unknown(monkeypatch, mock_mode, mock_device):
    """Replace mode does not need current list — succeeds even when allowed_vlans is None."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_trunk_vlans",
        lambda interface, device: {"existed": True, "mode": "trunk", "allowed_vlans": None},
    )
    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [10, 20]},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "completed"


def test_e2e_remove_would_empty_trunk_rejected(monkeypatch, mock_mode, mock_device):
    """Removing the only VLAN would empty the trunk — rejected before driver call."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_trunk_vlans",
        lambda interface, device: {"existed": True, "mode": "trunk", "allowed_vlans": [10]},
    )
    call_count = {"n": 0}

    def _spy(*args, **kwargs):
        call_count["n"] += 1
    monkeypatch.setattr(port_service, "set_trunk_allowed_vlans_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "remove", "vlans": [10]},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "failed"
    assert "blackout" in (job.error or "").lower() or "empty" in (job.error or "").lower()
    assert call_count["n"] == 0


def test_e2e_failure_triggers_rollback(monkeypatch, mock_mode, mock_device):
    """Force driver failure; verify rollback restores the prior VLAN list."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_trunk_vlans",
        lambda interface, device: {"existed": True, "mode": "trunk", "allowed_vlans": [1, 10]},
    )

    calls: list[list[int]] = []

    def _fake_set(interface, vlan_list, device):
        calls.append(sorted(vlan_list))
        # Forward call (desired=[20,30]) fails; rollback call ([1,10]) succeeds.
        if sorted(vlan_list) == [20, 30]:
            return {"rc": 1, "stdout": "", "stderr": "simulated failure", "success": False}
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    monkeypatch.setattr(port_service, "set_trunk_allowed_vlans_on_device", _fake_set)
    monkeypatch.setattr(
        port_service,
        "list_ports",
        lambda device: PortListResponse(
            device=device,
            vendor="huawei_vrp",
            ports=[PortInfo(name="Gi0/0/1", mode="trunk", allowed_vlans=[1, 10])],
        ),
    )
    monkeypatch.setattr("app.api.ports._RETRY_BASE_DELAY", 0.0)

    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi0/0/1", "mode": "replace", "vlans": [20, 30]},
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
    # Rollback restored the original list [1, 10]
    assert [1, 10] in calls


def test_e2e_unknown_interface_fails_cleanly(monkeypatch, mock_mode, mock_device):
    """Pre-state says the interface doesn't exist → fail cleanly, driver not called."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_trunk_vlans",
        lambda interface, device: {"existed": False, "mode": None, "allowed_vlans": None},
    )
    call_count = {"n": 0}

    def _spy(*args, **kwargs):
        call_count["n"] += 1
    monkeypatch.setattr(port_service, "set_trunk_allowed_vlans_on_device", _spy)

    res = _client("operator").patch(
        "/api/v1/ports/trunk-vlans",
        json={"device": "mock_device", "interface": "Gi9/9/9", "mode": "replace", "vlans": [10]},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]
    job = job_service.get_job(job_id)
    assert job.status == "failed"
    assert "does not exist" in (job.error or "").lower()
    assert call_count["n"] == 0
