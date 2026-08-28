"""DG2 regression test: the vendor driver is resolved at most once per
Device instance, cached across calls to different methods.
See docs/DEVICE_IMPLEMENTATION_PLAN.md §11.1 / §5 (DG2).
"""
from app.models.device import Device
from app.models.vlan import VLAN


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


class _CountingVlanDriver:
    instances_built = 0

    def __init__(self):
        _CountingVlanDriver.instances_built += 1

    def create_vlan(self, vlan_id, name, device, password):
        return {"rc": 0}

    def list_vlans(self, device, password):
        return []


def test_vlan_driver_resolved_once_across_methods(monkeypatch):
    monkeypatch.setattr("app.core.config.EXECUTION_MODE", "real")
    monkeypatch.setattr("app.services.secret_service.vault.decrypt", lambda enc: "pw")

    calls = []

    def _fake_get_driver(device):
        calls.append(device)
        return _CountingVlanDriver()

    monkeypatch.setattr("app.services.vendors.dispatcher.get_driver", _fake_get_driver)

    device = _make_device()
    device.create_vlan(VLAN(vlan_id=10, name="MGMT"))
    device.list_vlans()

    assert len(calls) == 1


class _CountingPortDriver:
    def list_ports(self, device, password):
        return []

    def set_port_admin_state(self, interface, enabled, device, password):
        return {"rc": 0}


def test_port_driver_resolved_once_across_methods(monkeypatch):
    monkeypatch.setattr("app.core.config.EXECUTION_MODE", "real")
    monkeypatch.setattr("app.services.secret_service.vault.decrypt", lambda enc: "pw")

    calls = []

    def _fake_get_port_driver(device):
        calls.append(device)
        return _CountingPortDriver()

    monkeypatch.setattr("app.services.vendors.dispatcher.get_port_driver", _fake_get_port_driver)

    device = _make_device()
    device.list_ports()
    device.set_port_admin_state("Gi0/0/1", True)

    assert len(calls) == 1


def test_password_decrypted_once_across_methods(monkeypatch):
    monkeypatch.setattr("app.core.config.EXECUTION_MODE", "real")
    monkeypatch.setattr("app.services.vendors.dispatcher.get_driver", lambda device: _CountingVlanDriver())

    decrypt_calls = []

    def _fake_decrypt(enc):
        decrypt_calls.append(enc)
        return "pw"

    monkeypatch.setattr("app.services.secret_service.vault.decrypt", _fake_decrypt)

    device = _make_device()
    device.create_vlan(VLAN(vlan_id=10, name="MGMT"))
    device.list_vlans()

    assert len(decrypt_calls) == 1
