"""Tests for the Step 3.1 port configuration domain model and vendor contract.

Covers:
* PortConfigRequest validation — valid combinations
* PortConfigRequest validation — invalid combinations / constraint violations
* PortConfigResult construction and serialization
* Dispatcher resolution (huawei / cisco / unknown vendor)
* Stub method presence and correct error type on HuaweiPortDriver + CiscoPortDriver
* BasePortDriver contract: configure_port / shutdown_port / enable_port all raise
  NotImplementedError from the base class default
"""

from __future__ import annotations

import pytest

from app.core.exceptions import UnsupportedVendorError
from app.models.device import Device
from app.models.port import PortConfigRequest, PortConfigResult
from app.services.vendors.cisco.port_driver import CiscoPortDriver
from app.services.vendors.dispatcher import get_port_vendor_driver
from app.services.vendors.huawei.port_driver import HuaweiPortDriver
from app.services.vendors.port_driver_base import BasePortDriver


# ── Helper ────────────────────────────────────────────────────────────────────

def _device(vendor: str = "huawei_vrp", platform: str = "vrp") -> Device:
    return Device(
        name="test_device",
        host="192.0.2.1",
        vendor=vendor,
        username="admin",
        encrypted_password="enc",
        platform=platform,
    )


# ── PortConfigRequest — valid combinations ────────────────────────────────────

def test_config_request_description_only():
    req = PortConfigRequest(device="sw1", interface="Gi0/1", description="UPLINK")
    assert req.description == "UPLINK"
    assert req.admin_enabled is None
    assert req.mode is None


def test_config_request_clear_description():
    req = PortConfigRequest(device="sw1", interface="Gi0/1", description="")
    assert req.description == ""
    assert "description" in req.mutation_fields


def test_config_request_admin_enabled_true():
    req = PortConfigRequest(device="sw1", interface="Gi0/1", admin_enabled=True)
    assert req.admin_enabled is True
    assert "admin_enabled" in req.mutation_fields


def test_config_request_admin_enabled_false():
    req = PortConfigRequest(device="sw1", interface="Gi0/1", admin_enabled=False)
    assert req.admin_enabled is False
    assert "admin_enabled" in req.mutation_fields


def test_config_request_mode_only():
    req = PortConfigRequest(device="sw1", interface="Gi0/1", mode="access")
    assert req.mode == "access"
    assert "mode" in req.mutation_fields


def test_config_request_access_mode_with_vlan():
    req = PortConfigRequest(
        device="sw1", interface="Gi0/1", mode="access", access_vlan=20
    )
    assert req.mode == "access"
    assert req.access_vlan == 20
    assert req.has_vlan_change is True


def test_config_request_trunk_mode_with_allowed_vlans():
    req = PortConfigRequest(
        device="sw1", interface="Gi0/1", mode="trunk", allowed_vlans=[10, 20, 30]
    )
    assert req.mode == "trunk"
    assert req.allowed_vlans == [10, 20, 30]
    assert req.has_vlan_change is True


def test_config_request_multiple_mutations():
    req = PortConfigRequest(
        device="sw1",
        interface="GigabitEthernet1/0/1",
        description="DESK",
        admin_enabled=True,
        mode="access",
        access_vlan=10,
    )
    fields = req.mutation_fields
    assert "description" in fields
    assert "admin_enabled" in fields
    assert "mode" in fields
    assert "access_vlan" in fields


def test_config_request_has_vlan_change_false_when_no_vlans():
    req = PortConfigRequest(device="sw1", interface="Gi0/1", description="X")
    assert req.has_vlan_change is False


# ── PortConfigRequest — invalid combinations ──────────────────────────────────

def test_config_request_empty_interface_raises():
    with pytest.raises(ValueError, match="interface"):
        PortConfigRequest(device="sw1", interface="", description="X")


def test_config_request_no_mutations_raises():
    with pytest.raises(ValueError, match="at least one mutation"):
        PortConfigRequest(device="sw1", interface="Gi0/1")


def test_config_request_access_vlan_without_mode_raises():
    with pytest.raises(ValueError, match="access_vlan.*mode="):
        PortConfigRequest(device="sw1", interface="Gi0/1", access_vlan=10)


def test_config_request_access_vlan_with_trunk_mode_raises():
    with pytest.raises(ValueError, match="access_vlan.*mode="):
        PortConfigRequest(
            device="sw1", interface="Gi0/1", mode="trunk", access_vlan=10
        )


def test_config_request_allowed_vlans_without_mode_raises():
    with pytest.raises(ValueError, match="allowed_vlans.*mode="):
        PortConfigRequest(
            device="sw1", interface="Gi0/1", allowed_vlans=[10, 20]
        )


def test_config_request_allowed_vlans_with_access_mode_raises():
    with pytest.raises(ValueError, match="allowed_vlans.*mode="):
        PortConfigRequest(
            device="sw1", interface="Gi0/1", mode="access", allowed_vlans=[10, 20]
        )


def test_config_request_both_vlan_fields_no_mode_raises():
    with pytest.raises(ValueError):
        PortConfigRequest(
            device="sw1", interface="Gi0/1", access_vlan=10, allowed_vlans=[20]
        )


# ── PortConfigResult — construction and serialization ────────────────────────

def test_config_result_minimal():
    result = PortConfigResult(success=True, changed=True, interface="Gi0/1")
    assert result.success is True
    assert result.changed is True
    assert result.interface == "Gi0/1"
    assert result.vendor is None
    assert result.execution_time_ms is None
    assert result.rollback_performed is None
    assert result.warnings is None


def test_config_result_full():
    result = PortConfigResult(
        success=False,
        changed=False,
        interface="GigabitEthernet1/0/1",
        vendor="huawei_vrp",
        execution_time_ms=1234.5,
        rollback_performed=True,
        warnings=["access_vlan already matched — skipped"],
    )
    assert result.vendor == "huawei_vrp"
    assert result.execution_time_ms == 1234.5
    assert result.rollback_performed is True
    assert result.warnings == ["access_vlan already matched — skipped"]


def test_config_result_to_dict_shape():
    result = PortConfigResult(
        success=True, changed=False, interface="Gi0/1", vendor="cisco_ios"
    )
    d = result.to_dict()
    assert set(d) == {
        "success", "changed", "interface", "vendor",
        "execution_time_ms", "rollback_performed", "warnings",
    }
    assert d["success"] is True
    assert d["changed"] is False
    assert d["vendor"] == "cisco_ios"
    assert d["warnings"] is None


def test_config_result_to_dict_warnings_list():
    result = PortConfigResult(
        success=True, changed=True, interface="Gi0/1",
        warnings=["noop on description"],
    )
    d = result.to_dict()
    assert d["warnings"] == ["noop on description"]


# ── Dispatcher resolution ─────────────────────────────────────────────────────

def test_dispatcher_huawei_vrp():
    driver = get_port_vendor_driver("huawei_vrp", "vrp")
    assert isinstance(driver, HuaweiPortDriver)


def test_dispatcher_huawei_alias():
    driver = get_port_vendor_driver("huawei", "vrp")
    assert isinstance(driver, HuaweiPortDriver)


def test_dispatcher_cisco_ios():
    driver = get_port_vendor_driver("cisco_ios", "ios")
    assert isinstance(driver, CiscoPortDriver)


def test_dispatcher_cisco_alias():
    driver = get_port_vendor_driver("cisco", "ios")
    assert isinstance(driver, CiscoPortDriver)


def test_dispatcher_unknown_raises_unsupported_vendor_error():
    with pytest.raises(UnsupportedVendorError) as exc_info:
        get_port_vendor_driver("juniper", "junos")
    assert exc_info.value.vendor == "juniper"
    assert exc_info.value.platform == "junos"


# ── BasePortDriver default stubs ──────────────────────────────────────────────
# The base class concrete implementations raise NotImplementedError — verify
# that neither Huawei nor Cisco subclasses inherit the base stubs (they must
# override them), and that the base stubs themselves raise correctly.

def test_base_driver_configure_port_raises():
    class _MinimalDriver(BasePortDriver):
        def list_ports(self, device, password):
            return []
    driver = _MinimalDriver()
    req = PortConfigRequest(device="sw1", interface="Gi0/1", description="x")
    with pytest.raises(NotImplementedError, match="configure_port"):
        driver.configure_port(req, _device(), "pw")


def test_base_driver_shutdown_port_raises():
    class _MinimalDriver(BasePortDriver):
        def list_ports(self, device, password):
            return []
    driver = _MinimalDriver()
    with pytest.raises(NotImplementedError, match="shutdown_port"):
        driver.shutdown_port("Gi0/1", _device(), "pw")


def test_base_driver_enable_port_raises():
    class _MinimalDriver(BasePortDriver):
        def list_ports(self, device, password):
            return []
    driver = _MinimalDriver()
    with pytest.raises(NotImplementedError, match="enable_port"):
        driver.enable_port("Gi0/1", _device(), "pw")


# ── HuaweiPortDriver stubs ────────────────────────────────────────────────────

def test_huawei_configure_port_raises_not_implemented():
    driver = HuaweiPortDriver()
    req = PortConfigRequest(device="sw1", interface="Gi0/0/1", description="x")
    with pytest.raises(NotImplementedError, match="configure_port"):
        driver.configure_port(req, _device(), "pw")


def test_huawei_shutdown_port_raises_not_implemented():
    driver = HuaweiPortDriver()
    with pytest.raises(NotImplementedError, match="shutdown_port"):
        driver.shutdown_port("GigabitEthernet0/0/1", _device(), "pw")


def test_huawei_enable_port_raises_not_implemented():
    driver = HuaweiPortDriver()
    with pytest.raises(NotImplementedError, match="enable_port"):
        driver.enable_port("GigabitEthernet0/0/1", _device(), "pw")


# ── CiscoPortDriver stubs ─────────────────────────────────────────────────────

def test_cisco_configure_port_raises_not_implemented():
    driver = CiscoPortDriver()
    req = PortConfigRequest(device="sw1", interface="Gi0/1", description="x")
    with pytest.raises(NotImplementedError, match="configure_port"):
        driver.configure_port(req, _device("cisco_ios", "ios"), "pw")


def test_cisco_shutdown_port_raises_not_implemented():
    driver = CiscoPortDriver()
    with pytest.raises(NotImplementedError, match="shutdown_port"):
        driver.shutdown_port("GigabitEthernet1/0/1", _device("cisco_ios", "ios"), "pw")


def test_cisco_enable_port_raises_not_implemented():
    driver = CiscoPortDriver()
    with pytest.raises(NotImplementedError, match="enable_port"):
        driver.enable_port("GigabitEthernet1/0/1", _device("cisco_ios", "ios"), "pw")


# ── Stub message quality ──────────────────────────────────────────────────────
# Verify the NotImplementedError messages are meaningful, not just empty.

@pytest.mark.parametrize("method,args", [
    ("shutdown_port", ("Gi0/1",)),
    ("enable_port", ("Gi0/1",)),
])
def test_huawei_stub_messages_are_informative(method, args):
    driver = HuaweiPortDriver()
    dev = _device()
    with pytest.raises(NotImplementedError) as exc_info:
        getattr(driver, method)(*args, dev, "pw")
    msg = str(exc_info.value)
    assert "HuaweiPortDriver" in msg
    assert method in msg


@pytest.mark.parametrize("method,args", [
    ("shutdown_port", ("Gi0/1",)),
    ("enable_port", ("Gi0/1",)),
])
def test_cisco_stub_messages_are_informative(method, args):
    driver = CiscoPortDriver()
    dev = _device("cisco_ios", "ios")
    with pytest.raises(NotImplementedError) as exc_info:
        getattr(driver, method)(*args, dev, "pw")
    msg = str(exc_info.value)
    assert "CiscoPortDriver" in msg
    assert method in msg
