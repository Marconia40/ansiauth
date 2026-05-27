"""Smoke tests for the port service layer (Step 1.1).

These tests focus on the orchestration contract — they do not exercise real
Ansible.  The Huawei driver path is covered by dedicated parser tests; here
we only confirm that mock mode returns the expected shape and that
dispatcher wiring resolves a Huawei device to ``HuaweiPortDriver``.
"""

import pytest

from app.core.exceptions import UnsupportedVendorError
from app.models.device import Device
from app.models.port import PortInfo, PortListResponse
from app.services import port_service
from app.services.vendors.cisco.port_driver import CiscoPortDriver
from app.services.vendors.dispatcher import get_port_driver, get_port_vendor_driver
from app.services.vendors.huawei.port_driver import HuaweiPortDriver
from app.services.vendors.port_driver_base import BasePortDriver


# ── Mock-mode (test env may default to real; force mock per-test) ────────────

@pytest.fixture
def mock_mode(monkeypatch):
    """Force ``port_service`` into mock execution mode for the duration of a test.

    The project's `.env` file can set ``EXECUTION_MODE=real`` for VLAN flows,
    so we explicitly override at the module level to keep these service-layer
    tests hermetic.
    """
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")


def test_mock_list_ports_returns_envelope(mock_mode):
    response = port_service.list_ports("mock_device")
    assert isinstance(response, PortListResponse)
    assert response.device == "mock_device"
    assert response.vendor == "mock"
    assert response.ports, "mock list should never be empty"
    for port in response.ports:
        assert isinstance(port, PortInfo)
        assert port.name


def test_mock_list_ports_envelope_to_dict_shape(mock_mode):
    response = port_service.list_ports("mock_device")
    payload = response.to_dict()
    assert payload["device"] == "mock_device"
    assert payload["vendor"] == "mock"
    assert payload["count"] == len(payload["ports"])
    for entry in payload["ports"]:
        # Every documented field is always present (None when unknown)
        for key in (
            "name", "description", "admin_up", "operational_up", "mode",
            "access_vlan", "allowed_vlans", "poe_enabled", "speed", "duplex",
        ):
            assert key in entry


def test_mock_list_ports_isolation_between_calls(mock_mode):
    """Mutation by a caller must not leak into the next ``list_ports`` call."""
    first = port_service.list_ports("mock_device").ports
    first[0].description = "MUTATED"
    second = port_service.list_ports("mock_device").ports
    assert second[0].description != "MUTATED"


def test_mock_get_port_returns_match(mock_mode):
    response = port_service.list_ports("mock_device")
    sample = response.ports[0]
    fetched = port_service.get_port("mock_device", sample.name)
    assert fetched is not None
    assert fetched.name == sample.name


def test_mock_get_port_returns_none_for_unknown_interface(mock_mode):
    assert port_service.get_port("mock_device", "DoesNotExist0/0/0") is None


def test_mock_list_ports_propagates_simulated_failure(mock_mode):
    with pytest.raises(RuntimeError):
        port_service.list_ports("fail_device")


# ── Dispatcher wiring ─────────────────────────────────────────────────────────

def test_dispatcher_resolves_huawei_strings():
    driver = get_port_vendor_driver("huawei", "vrp")
    assert isinstance(driver, HuaweiPortDriver)
    assert isinstance(driver, BasePortDriver)


def test_dispatcher_resolves_huawei_vrp_alias():
    driver = get_port_vendor_driver("huawei_vrp", "vrp")
    assert isinstance(driver, HuaweiPortDriver)


def test_dispatcher_raises_unsupported_vendor_for_unknown():
    with pytest.raises(UnsupportedVendorError) as exc:
        get_port_vendor_driver("juniper", "junos")
    # The exception must carry the raw strings so logs/handlers can render
    # diagnostic details even though the client-facing message stays generic.
    assert exc.value.vendor == "juniper"
    assert exc.value.platform == "junos"
    assert "junos" in str(exc.value)


def test_dispatcher_resolves_cisco_strings():
    driver = get_port_vendor_driver("cisco", "ios")
    assert isinstance(driver, CiscoPortDriver)
    assert isinstance(driver, BasePortDriver)


def test_dispatcher_resolves_cisco_ios_alias():
    driver = get_port_vendor_driver("cisco_ios", "ios")
    assert isinstance(driver, CiscoPortDriver)


def test_dispatcher_resolves_from_device_object():
    device = Device(
        name="huawei1",
        host="192.0.2.1",
        vendor="huawei_vrp",
        username="admin",
        encrypted_password="x",
        platform="vrp",
    )
    driver = get_port_driver(device)
    assert isinstance(driver, HuaweiPortDriver)
