"""Tests for the Step 3.2 Huawei VRP port write driver implementations.

Covers:
* shutdown_port — playbook invocation, extravars, success/failure mapping
* enable_port   — playbook invocation, extravars, success/failure mapping
* configure_port — extravars for every field combination
* configure_port — normalized PortConfigResult shape
* configure_port — Huawei VLAN list compression applied correctly
* configure_port — failure maps to success=False / changed=False
* configure_port — rollback_performed=None (driver doesn't set it; orchestration does)
* configure_port — field isolation (setting one field leaves others' flags False)
* configure_port — exception propagation (ansible_service errors bubble up)
"""

from __future__ import annotations

import pytest

from app.models.device import Device
from app.models.port import PortConfigRequest, PortConfigResult
from app.services.vendors.huawei.port_driver import (
    HuaweiPortDriver,
    _PLAYBOOK_CONFIGURE_PORT,
    _PLAYBOOK_ENABLE_PORT,
    _PLAYBOOK_SHUTDOWN_PORT,
)


# ── Fixtures / helpers ────────────────────────────────────────────────────────

def _device() -> Device:
    return Device(
        name="huawei1",
        host="192.0.2.1",
        vendor="huawei_vrp",
        username="admin",
        encrypted_password="encrypted",
        platform="vrp",
    )


def _ok(stdout: str = "OK") -> dict:
    return {"rc": 0, "stdout": stdout, "stderr": ""}


def _fail(stderr: str = "Error") -> dict:
    return {"rc": 1, "stdout": "", "stderr": stderr}


def _patch_playbook(monkeypatch, return_value: dict) -> list[dict]:
    """Replace ansible_service.run_playbook and return the captured call list."""
    calls: list[dict] = []

    def _mock(playbook, extravars, inventory):
        calls.append({"playbook": playbook, "extravars": extravars})
        return return_value

    monkeypatch.setattr("app.services.ansible_service.run_playbook", _mock)
    return calls


# ── shutdown_port ─────────────────────────────────────────────────────────────

def test_shutdown_port_calls_correct_playbook(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    HuaweiPortDriver().shutdown_port("GigabitEthernet0/0/1", _device(), "pw")
    assert calls[0]["playbook"] == _PLAYBOOK_SHUTDOWN_PORT


def test_shutdown_port_passes_interface_extravar(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    HuaweiPortDriver().shutdown_port("GigabitEthernet0/0/3", _device(), "pw")
    assert calls[0]["extravars"]["interface"] == "GigabitEthernet0/0/3"


def test_shutdown_port_passes_device_extravar(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    HuaweiPortDriver().shutdown_port("GigabitEthernet0/0/1", _device(), "pw")
    assert calls[0]["extravars"]["device"] == "huawei1"


def test_shutdown_port_success_maps_to_true(monkeypatch):
    _patch_playbook(monkeypatch, _ok())
    result = HuaweiPortDriver().shutdown_port("GigabitEthernet0/0/1", _device(), "pw")
    assert result["rc"] == 0
    assert result["success"] is True


def test_shutdown_port_failure_maps_to_false(monkeypatch):
    _patch_playbook(monkeypatch, _fail())
    result = HuaweiPortDriver().shutdown_port("GigabitEthernet0/0/1", _device(), "pw")
    assert result["rc"] == 1
    assert result["success"] is False


def test_shutdown_port_propagates_ansible_exception(monkeypatch):
    monkeypatch.setattr(
        "app.services.ansible_service.run_playbook",
        lambda **_kw: (_ for _ in ()).throw(RuntimeError("connection timeout")),
    )
    with pytest.raises(RuntimeError, match="connection timeout"):
        HuaweiPortDriver().shutdown_port("GigabitEthernet0/0/1", _device(), "pw")


# ── enable_port ───────────────────────────────────────────────────────────────

def test_enable_port_calls_correct_playbook(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    HuaweiPortDriver().enable_port("GigabitEthernet0/0/1", _device(), "pw")
    assert calls[0]["playbook"] == _PLAYBOOK_ENABLE_PORT


def test_enable_port_passes_interface_extravar(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    HuaweiPortDriver().enable_port("GigabitEthernet0/0/5", _device(), "pw")
    assert calls[0]["extravars"]["interface"] == "GigabitEthernet0/0/5"


def test_enable_port_success_maps_to_true(monkeypatch):
    _patch_playbook(monkeypatch, _ok())
    result = HuaweiPortDriver().enable_port("GigabitEthernet0/0/1", _device(), "pw")
    assert result["rc"] == 0
    assert result["success"] is True


def test_enable_port_failure_maps_to_false(monkeypatch):
    _patch_playbook(monkeypatch, _fail())
    result = HuaweiPortDriver().enable_port("GigabitEthernet0/0/1", _device(), "pw")
    assert result["rc"] == 1
    assert result["success"] is False


def test_shutdown_and_enable_use_distinct_playbooks(monkeypatch):
    shutdown_calls = _patch_playbook(monkeypatch, _ok())
    HuaweiPortDriver().shutdown_port("Gi0/0/1", _device(), "pw")
    shutdown_pb = shutdown_calls[0]["playbook"]

    enable_calls = _patch_playbook(monkeypatch, _ok())
    HuaweiPortDriver().enable_port("Gi0/0/1", _device(), "pw")
    enable_pb = enable_calls[0]["playbook"]

    assert shutdown_pb != enable_pb
    assert "shutdown" in shutdown_pb
    assert "enable" in enable_pb


# ── configure_port — correct playbook ────────────────────────────────────────

def test_configure_port_calls_configure_playbook(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", description="X")
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    assert calls[0]["playbook"] == _PLAYBOOK_CONFIGURE_PORT


# ── configure_port — field isolation (description only) ──────────────────────

def test_configure_port_description_only_sets_correct_flags(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", description="UPLINK")
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    ev = calls[0]["extravars"]

    assert ev["configure_description"] is True
    assert ev["description"] == "UPLINK"
    assert ev["description_is_empty"] is False
    assert ev["configure_admin"] is False
    assert ev["configure_mode"] is False
    assert ev["configure_access_vlan"] is False
    assert ev["configure_trunk_vlans"] is False


def test_configure_port_clear_description_sets_empty_flag(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", description="")
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    ev = calls[0]["extravars"]

    assert ev["configure_description"] is True
    assert ev["description_is_empty"] is True


# ── configure_port — admin state ──────────────────────────────────────────────

def test_configure_port_admin_disable_flags(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", admin_enabled=False)
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    ev = calls[0]["extravars"]

    assert ev["configure_admin"] is True
    assert ev["admin_enabled"] is False
    assert ev["configure_description"] is False
    assert ev["configure_mode"] is False


def test_configure_port_admin_enable_flags(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", admin_enabled=True)
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    ev = calls[0]["extravars"]

    assert ev["configure_admin"] is True
    assert ev["admin_enabled"] is True


# ── configure_port — mode + VLAN assignment ───────────────────────────────────

def test_configure_port_access_mode_and_vlan(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(
        device="huawei1", interface="Gi0/0/1", mode="access", access_vlan=20
    )
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    ev = calls[0]["extravars"]

    assert ev["configure_mode"] is True
    assert ev["mode"] == "access"
    assert ev["configure_access_vlan"] is True
    assert ev["access_vlan"] == 20
    assert ev["configure_trunk_vlans"] is False


def test_configure_port_trunk_mode_and_vlans(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(
        device="huawei1", interface="Gi0/0/24", mode="trunk", allowed_vlans=[10, 11, 12, 20]
    )
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    ev = calls[0]["extravars"]

    assert ev["configure_mode"] is True
    assert ev["mode"] == "trunk"
    assert ev["configure_trunk_vlans"] is True
    assert ev["vlan_list"] == "10 to 12 20"   # Huawei range compression
    assert ev["configure_access_vlan"] is False


def test_configure_port_mode_only_no_vlan_flags(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", mode="trunk")
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    ev = calls[0]["extravars"]

    assert ev["configure_mode"] is True
    assert ev["configure_access_vlan"] is False
    assert ev["configure_trunk_vlans"] is False


def test_configure_port_trunk_vlan_compression_single(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(
        device="huawei1", interface="Gi0/0/24", mode="trunk", allowed_vlans=[100]
    )
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    assert calls[0]["extravars"]["vlan_list"] == "100"


def test_configure_port_trunk_vlan_compression_multiple_ranges(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(
        device="huawei1", interface="Gi0/0/24", mode="trunk",
        allowed_vlans=[10, 11, 12, 20, 30, 31],
    )
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    assert calls[0]["extravars"]["vlan_list"] == "10 to 12 20 30 to 31"


# ── configure_port — multi-field composite ────────────────────────────────────

def test_configure_port_description_and_admin_together(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(
        device="huawei1", interface="Gi0/0/1",
        description="DESK", admin_enabled=False,
    )
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    ev = calls[0]["extravars"]

    assert ev["configure_description"] is True
    assert ev["description"] == "DESK"
    assert ev["configure_admin"] is True
    assert ev["admin_enabled"] is False


def test_configure_port_full_access_composite(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(
        device="huawei1", interface="Gi0/0/1",
        description="USER-PC", admin_enabled=True, mode="access", access_vlan=30,
    )
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    ev = calls[0]["extravars"]

    assert ev["configure_description"] is True
    assert ev["configure_admin"] is True
    assert ev["configure_mode"] is True
    assert ev["configure_access_vlan"] is True
    assert ev["access_vlan"] == 30
    assert ev["configure_trunk_vlans"] is False


# ── configure_port — normalized PortConfigResult shape ───────────────────────

def test_configure_port_success_result_shape(monkeypatch):
    _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", description="X")
    result = HuaweiPortDriver().configure_port(req, _device(), "pw")

    assert isinstance(result, PortConfigResult)
    assert result.success is True
    assert result.changed is True
    assert result.interface == "Gi0/0/1"
    assert result.vendor == "huawei_vrp"
    assert result.execution_time_ms is not None
    assert result.execution_time_ms >= 0
    assert result.rollback_performed is None   # orchestration layer sets this
    assert result.warnings is None


def test_configure_port_failure_result_shape(monkeypatch):
    _patch_playbook(monkeypatch, _fail("device refused command"))
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", description="X")
    result = HuaweiPortDriver().configure_port(req, _device(), "pw")

    assert isinstance(result, PortConfigResult)
    assert result.success is False
    assert result.changed is False
    assert result.vendor == "huawei_vrp"
    assert result.rollback_performed is None   # not set by driver


def test_configure_port_result_to_dict_is_complete(monkeypatch):
    _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/2", description="Y")
    result = HuaweiPortDriver().configure_port(req, _device(), "pw")
    d = result.to_dict()

    expected_keys = {
        "success", "changed", "interface", "vendor",
        "execution_time_ms", "rollback_performed", "warnings",
    }
    assert set(d) == expected_keys


# ── configure_port — rollback compatibility ───────────────────────────────────

def test_configure_port_rollback_performed_not_set_by_driver_on_success(monkeypatch):
    _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", description="X")
    result = HuaweiPortDriver().configure_port(req, _device(), "pw")
    assert result.rollback_performed is None


def test_configure_port_rollback_performed_not_set_by_driver_on_failure(monkeypatch):
    _patch_playbook(monkeypatch, _fail())
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", description="X")
    result = HuaweiPortDriver().configure_port(req, _device(), "pw")
    # Driver returns None; orchestration layer sets True/False after attempting rollback
    assert result.rollback_performed is None


def test_configure_port_exception_propagates_for_orchestration_rollback(monkeypatch):
    """Ansible connection failure must propagate so orchestration can catch and rollback."""
    def _raise(**_):
        raise RuntimeError("SSH unreachable")

    monkeypatch.setattr("app.services.ansible_service.run_playbook", _raise)
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", description="X")
    with pytest.raises(RuntimeError, match="SSH unreachable"):
        HuaweiPortDriver().configure_port(req, _device(), "pw")


# ── configure_port — interface and device propagation ────────────────────────

def test_configure_port_interface_propagated_to_result(monkeypatch):
    _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(
        device="huawei1", interface="GigabitEthernet0/0/24", description="UPLINK"
    )
    result = HuaweiPortDriver().configure_port(req, _device(), "pw")
    assert result.interface == "GigabitEthernet0/0/24"


def test_configure_port_device_name_in_extravars(monkeypatch):
    calls = _patch_playbook(monkeypatch, _ok())
    req = PortConfigRequest(device="huawei1", interface="Gi0/0/1", description="X")
    HuaweiPortDriver().configure_port(req, _device(), "pw")
    assert calls[0]["extravars"]["device"] == "huawei1"
