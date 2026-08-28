"""Constructor-injection tests: every Device method is a one-line delegate.

No monkeypatch, no Inventory involved — a fake driver is assigned directly
to Device._vlan_driver / Device._port_driver (both are plain private
attributes, not dataclass-init fields), matching
docs/DEVICE_IMPLEMENTATION_PLAN.md §11.1.
"""
from app.models.device import Device
from app.models.port import PortConfigRequest, PortConfigResult
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
    device = Device(**defaults)
    device._password = "plaintext-pw"  # skip secret_service for these tests
    return device


class _FakeVlanDriver:
    def __init__(self):
        self.calls = {}

    def create_vlan(self, vlan_id, name, device, password):
        self.calls["create_vlan"] = (vlan_id, name, device, password)
        return {"rc": 0, "stdout": "created", "stderr": ""}

    def delete_vlan(self, vlan_id, device, password):
        self.calls["delete_vlan"] = (vlan_id, device, password)
        return {"rc": 0, "stdout": "deleted", "stderr": ""}

    def update_vlan(self, vlan_id, name, device, password):
        self.calls["update_vlan"] = (vlan_id, name, device, password)
        return {"rc": 0, "stdout": "updated", "stderr": ""}

    def save_config(self, device, password):
        self.calls["save_config"] = (device, password)
        return {"rc": 0, "stdout": "saved", "stderr": ""}

    def list_vlans(self, device, password):
        self.calls["list_vlans"] = (device, password)
        return ["vlan-list-sentinel"]


class _FakePortDriver:
    def __init__(self):
        self.calls = {}

    def configure_port(self, config, device, password):
        self.calls["configure_port"] = (config, device, password)
        return PortConfigResult(success=True, changed=True, interface=config.interface)

    def set_port_admin_state(self, interface, enabled, device, password):
        self.calls["set_port_admin_state"] = (interface, enabled, device, password)
        return {"rc": 0, "success": True}

    def set_port_access_vlan(self, interface, vlan_id, device, password):
        self.calls["set_port_access_vlan"] = (interface, vlan_id, device, password)
        return {"rc": 0, "success": True}

    def set_trunk_allowed_vlans(self, interface, vlan_list, device, password):
        self.calls["set_trunk_allowed_vlans"] = (interface, vlan_list, device, password)
        return {"rc": 0, "success": True}

    def update_port_description(self, interface, description, device, password):
        self.calls["update_port_description"] = (interface, description, device, password)
        return {"rc": 0, "success": True}

    def list_ports(self, device, password):
        self.calls["list_ports"] = (device, password)
        return ["port-list-sentinel"]


def test_create_vlan_delegates_to_driver():
    device = _make_device()
    fake = _FakeVlanDriver()
    device._vlan_driver = fake

    result = device.create_vlan(VLAN(vlan_id=10, name="MGMT"))

    assert result == {"rc": 0, "stdout": "created", "stderr": ""}
    assert fake.calls["create_vlan"] == (10, "MGMT", device, "plaintext-pw")


def test_delete_vlan_delegates_to_driver():
    device = _make_device()
    fake = _FakeVlanDriver()
    device._vlan_driver = fake

    result = device.delete_vlan(VLAN(vlan_id=10))

    assert result == {"rc": 0, "stdout": "deleted", "stderr": ""}
    assert fake.calls["delete_vlan"] == (10, device, "plaintext-pw")


def test_update_vlan_description_delegates_to_driver():
    device = _make_device()
    fake = _FakeVlanDriver()
    device._vlan_driver = fake

    result = device.update_vlan_description(VLAN(vlan_id=10, name="new-description"))

    assert result == {"rc": 0, "stdout": "updated", "stderr": ""}
    assert fake.calls["update_vlan"] == (10, "new-description", device, "plaintext-pw")


def test_save_config_delegates_to_driver():
    device = _make_device()
    fake = _FakeVlanDriver()
    device._vlan_driver = fake

    result = device.save_config()

    assert result == {"rc": 0, "stdout": "saved", "stderr": ""}
    assert fake.calls["save_config"] == (device, "plaintext-pw")


def test_list_vlans_delegates_to_driver():
    device = _make_device()
    fake = _FakeVlanDriver()
    device._vlan_driver = fake

    assert device.list_vlans() == ["vlan-list-sentinel"]
    assert fake.calls["list_vlans"] == (device, "plaintext-pw")


def test_configure_port_delegates_to_driver():
    device = _make_device()
    fake = _FakePortDriver()
    device._port_driver = fake
    config = PortConfigRequest(device="sw1", interface="Gi0/0/1", admin_enabled=True)

    result = device.configure_port(config)

    assert result.success is True
    assert fake.calls["configure_port"] == (config, device, "plaintext-pw")


def test_set_port_admin_state_delegates_to_driver():
    device = _make_device()
    fake = _FakePortDriver()
    device._port_driver = fake

    result = device.set_port_admin_state("Gi0/0/1", True)

    assert result == {"rc": 0, "success": True}
    assert fake.calls["set_port_admin_state"] == ("Gi0/0/1", True, device, "plaintext-pw")


def test_set_port_access_vlan_delegates_to_driver():
    device = _make_device()
    fake = _FakePortDriver()
    device._port_driver = fake

    result = device.set_port_access_vlan("Gi0/0/1", 20)

    assert result == {"rc": 0, "success": True}
    assert fake.calls["set_port_access_vlan"] == ("Gi0/0/1", 20, device, "plaintext-pw")


def test_set_trunk_allowed_vlans_delegates_to_driver():
    device = _make_device()
    fake = _FakePortDriver()
    device._port_driver = fake

    result = device.set_trunk_allowed_vlans("Gi0/0/24", [10, 20, 30])

    assert result == {"rc": 0, "success": True}
    assert fake.calls["set_trunk_allowed_vlans"] == ("Gi0/0/24", [10, 20, 30], device, "plaintext-pw")


def test_update_port_description_delegates_to_driver():
    device = _make_device()
    fake = _FakePortDriver()
    device._port_driver = fake

    result = device.update_port_description("Gi0/0/1", "uplink")

    assert result == {"rc": 0, "success": True}
    assert fake.calls["update_port_description"] == ("Gi0/0/1", "uplink", device, "plaintext-pw")


def test_list_ports_delegates_to_driver():
    device = _make_device()
    fake = _FakePortDriver()
    device._port_driver = fake

    assert device.list_ports() == ["port-list-sentinel"]
    assert fake.calls["list_ports"] == (device, "plaintext-pw")
