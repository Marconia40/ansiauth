"""VLAN is one class for both the read side (what parsers build from a real
device) and the write side (what Device.create_vlan/delete_vlan/
update_vlan_description accept). See docs/DEVICE_IMPLEMENTATION_PLAN.md D7.

vlan_id (range + not-reserved) validates unconditionally at construction —
safe for both paths, since parsers already exclude reserved VLANs before
building one. name format does NOT validate at construction (real devices
can report names/descriptions with characters the strict input-validation
regex would reject, e.g. spaces) — it validates only via the explicit
validate_name(), which Device calls before create/update, never delete.
"""
import pytest

from app.models.device import Device
from app.models.vlan import VLAN


def test_vlan_id_out_of_range_raises_on_construction():
    with pytest.raises(ValueError):
        VLAN(vlan_id=0)
    with pytest.raises(ValueError):
        VLAN(vlan_id=4095)


def test_vlan_reserved_id_raises_on_construction():
    for reserved in (1, 1002, 1003, 1004, 1005):
        with pytest.raises(ValueError):
            VLAN(vlan_id=reserved)


def test_vlan_construction_never_validates_name():
    """A name a real device might report (e.g. with a space) must not
    prevent building the object — that would break reading real VLANs."""
    vlan = VLAN(vlan_id=10, name="Guest Network")
    assert vlan.name == "Guest Network"


def test_vlan_without_name_is_valid_for_delete():
    vlan = VLAN(vlan_id=10)
    assert vlan.name == ""


def test_validate_name_rejects_bad_format():
    vlan = VLAN(vlan_id=10, name="Guest Network")
    with pytest.raises(ValueError):
        vlan.validate_name()


def test_validate_name_accepts_good_format():
    vlan = VLAN(vlan_id=10, name="Guest-Network")
    vlan.validate_name()  # must not raise


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
    device._password = "plaintext-pw"
    return device


class _FakeVlanDriver:
    def __init__(self):
        self.calls = {}

    def create_vlan(self, vlan_id, name, device, password):
        self.calls["create_vlan"] = (vlan_id, name)
        return {"rc": 0}

    def delete_vlan(self, vlan_id, device, password):
        self.calls["delete_vlan"] = vlan_id
        return {"rc": 0}

    def update_vlan(self, vlan_id, name, device, password):
        self.calls["update_vlan"] = (vlan_id, name)
        return {"rc": 0}


def test_device_create_vlan_rejects_bad_name_before_calling_driver():
    device = _make_device()
    fake = _FakeVlanDriver()
    device._vlan_driver = fake

    with pytest.raises(ValueError):
        device.create_vlan(VLAN(vlan_id=10, name="Guest Network"))

    assert "create_vlan" not in fake.calls


def test_device_update_vlan_description_rejects_bad_name_before_calling_driver():
    device = _make_device()
    fake = _FakeVlanDriver()
    device._vlan_driver = fake

    with pytest.raises(ValueError):
        device.update_vlan_description(VLAN(vlan_id=10, name="Guest Network"))

    assert "update_vlan" not in fake.calls


def test_device_delete_vlan_never_validates_name():
    """delete_vlan doesn't need a name — a VLAN built with only vlan_id
    (the delete use case) must work even though .name is empty."""
    device = _make_device()
    fake = _FakeVlanDriver()
    device._vlan_driver = fake

    result = device.delete_vlan(VLAN(vlan_id=10))

    assert result == {"rc": 0}
    assert fake.calls["delete_vlan"] == 10
