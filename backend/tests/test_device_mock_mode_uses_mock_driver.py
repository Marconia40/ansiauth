"""Under EXECUTION_MODE=mock, a plain Device (no Inventory) resolves the
mock drivers on first use. See docs/DEVICE_IMPLEMENTATION_PLAN.md §11.1.
"""
from app.models.device import Device
from app.models.vlan import VLAN
from app.services.vendors.mock import MockPortDriver, MockVlanDriver


def _make_device(**overrides) -> Device:
    defaults = dict(
        name="sw1",
        host="10.0.0.1",
        vendor="huawei_vrp",
        username="admin",
        encrypted_password="enc",
    )
    defaults.update(overrides)
    return Device(**defaults)


def test_mock_mode_resolves_mock_vlan_driver(monkeypatch):
    monkeypatch.setattr("app.core.config.EXECUTION_MODE", "mock")
    device = _make_device()

    result = device.create_vlan(VLAN(vlan_id=10, name="MGMT"))

    assert isinstance(device._vlan_driver, MockVlanDriver)
    assert result["rc"] == 0


def test_mock_mode_resolves_mock_port_driver(monkeypatch):
    monkeypatch.setattr("app.core.config.EXECUTION_MODE", "mock")
    device = _make_device()

    ports = device.list_ports()

    assert isinstance(device._port_driver, MockPortDriver)
    assert len(ports) > 0


def test_mock_mode_never_decrypts_password(monkeypatch):
    """secret_service.decrypt_password must not run in mock mode (§6.1)."""
    monkeypatch.setattr("app.core.config.EXECUTION_MODE", "mock")

    def _boom(*_a, **_kw):
        raise AssertionError("decrypt_password must not be called in mock mode")

    monkeypatch.setattr("app.services.secret_service.SecretVault.decrypt", _boom)
    device = _make_device()

    device.create_vlan(VLAN(vlan_id=10, name="MGMT"))

    assert device._get_password() is None
