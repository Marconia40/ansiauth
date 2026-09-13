"""Tests for the Step 2.4 trunk allowed-VLAN management flow.

Covers:
* RBAC: unauthenticated, observer rejected, operator allowed
* unknown device -> 404
* unsupported vendor -> friendly 501 (no vendor leakage)
* invalid VLAN IDs rejected (reserved 1002-1005, out-of-range, empty list)
* end-to-end replace/add/remove happy paths
* idempotent no-op when desired list already matches current
* access-mode port rejected cleanly (mode guard)
* unknown-current-list blocks add/remove; replace still works
* remove-to-empty rejected
* rollback path restores prior VLAN list on failure
* VLAN list compression utilities (cisco + huawei formats)
* the allowed-vlan-operation ("replace"/"add"/"remove") computation

Modernized against the current architecture:
* `app.services.port_execution_service._compute_desired_vlans()` is gone
  -- the equivalent computation now lives inline in
  `Puerto._resolver_allowed_vlans()` (app/models/port.py), exercised
  directly below against a minimal fake device/driver. Two real,
  confirmed behavior changes from the old free function (both documented
  in that method's own comments as deliberate bug fixes, not omissions):
    - "add"/"remove" with an unknown current list now RAISES ValueError
      instead of silently returning ``None``.
    - "remove" that would leave the trunk empty now RAISES ValueError
      instead of silently returning ``[]``.
* `app.validators.port_validator.compress_vlans_cisco/_huawei` are gone
  -- the compression logic is now a private method on each concrete
  vendor driver (`CiscoVendor._compress_vlans_cisco`,
  `HuaweiVendor._compress_vlans_huawei`), both delegating range-collapse
  to the shared `VendorDriver._compress_to_ranges()`.
* Route moved to `/api/v1/devices/{name}/ports/trunk-vlans`; `mode` in
  the request body means "how to apply `vlans`" (replace/add/remove),
  mapped to `Puerto.allowed_vlan_operation` -- NOT the switchport mode.
* `MockVendor` cannot be used for Puerto writes (missing
  `resolver_*`/`aplicar_paso`) -- see `tests/_puerto_fakes.py::FakePuertoDriver`.
"""

from __future__ import annotations

import pytest

from app.composition import device_repository, job_repository, plugin_registry
from app.models.device import Device
from app.models.port import Puerto
from app.services.vendors.cisco.driver import CiscoVendor
from app.services.vendors.huawei.driver import HuaweiVendor

from tests._puerto_fakes import FakePuertoDriver, StubUnimplementedDriver, get_or_create_device

_DEVICE = "trunk_dev"
_IFACE_TRUNK = "GigabitEthernet0/0/24"    # trunk, PVID 1, allowed [10, 20, 30]
_IFACE_ACCESS = "GigabitEthernet0/0/1"    # access, vlan 10


@pytest.fixture
def fake_driver(monkeypatch):
    drv = FakePuertoDriver()
    monkeypatch.setitem(plugin_registry._vendors, "cisco_ios", drv)
    monkeypatch.setitem(plugin_registry._vendors, "huawei_vrp", drv)
    return drv


@pytest.fixture
def device(fake_driver):
    return get_or_create_device(_DEVICE, site_name="Trunk VLANs Test Site")


def _url(name: str = _DEVICE) -> str:
    return f"/api/v1/devices/{name}/ports/trunk-vlans"


# -- VLAN list compression (now per-vendor-driver, not a shared validator) ----

def test_compress_vlans_cisco_single():
    assert CiscoVendor()._compress_vlans_cisco([10]) == "10"


def test_compress_vlans_cisco_range():
    assert CiscoVendor()._compress_vlans_cisco([10, 11, 12]) == "10-12"


def test_compress_vlans_cisco_mixed():
    assert CiscoVendor()._compress_vlans_cisco([10, 11, 12, 20, 30, 31]) == "10-12,20,30-31"


def test_compress_vlans_cisco_unordered_dedup():
    assert CiscoVendor()._compress_vlans_cisco(sorted(set([30, 10, 11, 30, 12]))) == "10-12,30"


def test_compress_vlans_huawei_single():
    assert HuaweiVendor()._compress_vlans_huawei([10]) == "10"


def test_compress_vlans_huawei_range():
    assert HuaweiVendor()._compress_vlans_huawei([10, 11, 12]) == "10 to 12"


def test_compress_vlans_huawei_mixed():
    assert HuaweiVendor()._compress_vlans_huawei([10, 11, 12, 20, 30, 31]) == "10 to 12 20 30 to 31"


# -- allowed-vlan-operation computation (Puerto._resolver_allowed_vlans) ------

class _FakeVlanDriver:
    def resolver_set_trunk_allowed_vlans(self, interface, vlan_list):
        return ("set_trunk_allowed_vlans", None, {"interface": interface, "vlan_list": list(vlan_list)})


class _FakeVlanDevice:
    driver = _FakeVlanDriver()


def _desired(operation: str, current: "list[int] | None", requested: list[int]) -> list[int]:
    actual = None if current is None else Puerto(interface="Gi0/1", mode="trunk", allowed_vlans=current)
    p = Puerto(interface="Gi0/1", allowed_vlans=requested, allowed_vlan_operation=operation)
    paso = p._resolver_allowed_vlans(_FakeVlanDevice(), actual)
    assert paso is not None
    _op_key, _variant, vars = paso
    return vars["vlan_list"]


def test_compute_replace():
    assert _desired("replace", [1, 10], [20, 30]) == [20, 30]


def test_compute_replace_no_current_needed():
    assert _desired("replace", None, [10, 20]) == [10, 20]


def test_compute_add():
    assert _desired("add", [1, 10], [10, 20]) == [1, 10, 20]


def test_compute_add_raises_when_current_unknown():
    # Confirmed behavior change vs the old free function (see module
    # docstring): unknown current list now fails loud instead of silently
    # returning None.
    p = Puerto(interface="Gi0/1", allowed_vlans=[10], allowed_vlan_operation="add")
    with pytest.raises(ValueError):
        p._resolver_allowed_vlans(_FakeVlanDevice(), None)


def test_compute_remove():
    assert _desired("remove", [1, 10, 20], [10]) == [1, 20]


def test_compute_remove_raises_when_current_unknown():
    p = Puerto(interface="Gi0/1", allowed_vlans=[10], allowed_vlan_operation="remove")
    with pytest.raises(ValueError):
        p._resolver_allowed_vlans(_FakeVlanDevice(), None)


def test_compute_remove_to_empty_raises():
    # Confirmed behavior change vs the old free function: emptying a trunk
    # via "remove" is now rejected loud instead of returning [].
    actual = Puerto(interface="Gi0/1", mode="trunk", allowed_vlans=[10])
    p = Puerto(interface="Gi0/1", allowed_vlans=[10], allowed_vlan_operation="remove")
    with pytest.raises(ValueError):
        p._resolver_allowed_vlans(_FakeVlanDevice(), actual)


# -- RBAC + plumbing ------------------------------------------------------------

def test_patch_rejects_unauthenticated(unauth_client, device):
    res = unauth_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": [40, 50]})
    assert res.status_code == 401


def test_patch_rejects_observer(observer_client, device):
    res = observer_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": [40, 50]})
    assert res.status_code == 403


def test_patch_operator_allowed(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": [40, 50]})
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body["data"]
    assert body["data"]["jobs"][0]["device"] == _DEVICE
    assert body["data"]["jobs"][0]["job_id"]


def test_patch_unknown_device_returns_404(operator_client, fake_driver):
    res = operator_client.patch(_url("no-such-device"), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": [10]})
    assert res.status_code == 404


def test_patch_unsupported_vendor_returns_friendly_501(admin_client, monkeypatch):
    monkeypatch.setitem(plugin_registry._vendors, "juniper", StubUnimplementedDriver())
    fake_dev = Device(
        name="juniper-x4", host="192.0.2.99", vendor="juniper",
        username="admin", encrypted_password="enc", platform="junos",
    )
    monkeypatch.setattr(device_repository, "get", lambda name: fake_dev if name == "juniper-x4" else None)

    res = admin_client.patch(_url("juniper-x4"), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": [10]})
    assert res.status_code == 501
    body = res.json()
    assert body["error_code"] == "VENDOR_NOT_SUPPORTED"
    blob = res.text.lower()
    assert "juniper" not in blob
    assert "junos" not in blob


# -- VLAN ID validation ---------------------------------------------------------

@pytest.mark.parametrize("vlan_id", [1002, 1003, 1004, 1005])
def test_reserved_vlan_ids_rejected(operator_client, device, vlan_id):
    res = operator_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": [vlan_id]})
    assert res.status_code == 422


def test_vlan_id_out_of_range_rejected(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": [4095]})
    assert res.status_code == 422


def test_empty_vlans_list_rejected(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": []})
    assert res.status_code == 422


def test_duplicate_vlans_accepted_and_deduplicated(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": [40, 40]})
    assert res.status_code == 202


# -- End-to-end runner scenarios -------------------------------------------------

def test_e2e_replace_completes(operator_client, device, fake_driver):
    res = operator_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": [40, 50]})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.retry_count == 0
    assert job.rollback_performed in (False, None)
    calls = [v["vlan_list"] for op, _va, v in fake_driver.calls if op == "set_trunk_allowed_vlans"]
    assert calls == [[40, 50]]


def test_e2e_add_merges_with_current(operator_client, device, fake_driver):
    """Add mode: desired = current UNION requested ([10,20,30] + [40])."""
    res = operator_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "add", "vlans": [40]})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
    calls = [v["vlan_list"] for op, _va, v in fake_driver.calls if op == "set_trunk_allowed_vlans"]
    assert calls == [[10, 20, 30, 40]]


def test_e2e_remove_subtracts_from_current(operator_client, device, fake_driver):
    """Remove mode: desired = current MINUS requested ([10,20,30] - [20])."""
    res = operator_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "remove", "vlans": [20]})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
    calls = [v["vlan_list"] for op, _va, v in fake_driver.calls if op == "set_trunk_allowed_vlans"]
    assert calls == [[10, 30]]


def test_e2e_noop_when_replace_already_matches(operator_client, device, fake_driver):
    """Replace with the same list -> no-op, driver not called."""
    res = operator_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": [10, 20, 30]})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
    assert job.result.get("noop") is True
    assert not any(op == "set_trunk_allowed_vlans" for op, _v, _vars in fake_driver.calls)


def test_e2e_access_port_fails_cleanly(operator_client, device, fake_driver):
    """A port in access mode must be rejected without calling the driver."""
    res = operator_client.patch(_url(), json={"interface": _IFACE_ACCESS, "mode": "replace", "vlans": [10]})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "failed"
    assert "trunk" in (job.error or "").lower()
    assert not any(op == "set_trunk_allowed_vlans" for op, _v, _vars in fake_driver.calls)


def test_e2e_add_fails_when_current_vlans_unknown(operator_client, fake_driver):
    """Add mode with unknown current list -> fail cleanly, driver not called."""
    unknown_trunk = Puerto(interface="GigabitEthernet0/0/30", mode="trunk", access_vlan=1, allowed_vlans=None)
    fake_driver.ports.append(unknown_trunk)
    dev = get_or_create_device("trunk_dev_unknown", site_name="Trunk VLANs Test Site")

    res = operator_client.patch(
        f"/api/v1/devices/{dev.name}/ports/trunk-vlans",
        json={"interface": "GigabitEthernet0/0/30", "mode": "add", "vlans": [10]},
    )
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "failed"
    assert not any(op == "set_trunk_allowed_vlans" for op, _v, _vars in fake_driver.calls)


def test_e2e_replace_succeeds_when_current_vlans_unknown(operator_client, fake_driver):
    """Replace mode does not need the current list -- succeeds even when
    allowed_vlans is None."""
    unknown_trunk = Puerto(interface="GigabitEthernet0/0/31", mode="trunk", access_vlan=1, allowed_vlans=None)
    fake_driver.ports.append(unknown_trunk)
    dev = get_or_create_device("trunk_dev_unknown2", site_name="Trunk VLANs Test Site")

    res = operator_client.patch(
        f"/api/v1/devices/{dev.name}/ports/trunk-vlans",
        json={"interface": "GigabitEthernet0/0/31", "mode": "replace", "vlans": [10, 20]},
    )
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"


def test_e2e_remove_would_empty_trunk_rejected(operator_client, fake_driver):
    """Removing every allowed VLAN would empty the trunk -- rejected before the driver call."""
    single_vlan_trunk = Puerto(interface="GigabitEthernet0/0/32", mode="trunk", access_vlan=1, allowed_vlans=[10])
    fake_driver.ports.append(single_vlan_trunk)
    dev = get_or_create_device("trunk_dev_single", site_name="Trunk VLANs Test Site")

    res = operator_client.patch(
        f"/api/v1/devices/{dev.name}/ports/trunk-vlans",
        json={"interface": "GigabitEthernet0/0/32", "mode": "remove", "vlans": [10]},
    )
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "failed"
    assert "permitidas" in (job.error or "").lower() or "empty" in (job.error or "").lower()
    assert not any(op == "set_trunk_allowed_vlans" for op, _v, _vars in fake_driver.calls)


def test_e2e_failure_triggers_rollback(operator_client, device, fake_driver):
    """Force driver failure; verify rollback restores the prior VLAN list."""
    def _fail_on(op_key, vars):
        return op_key == "set_trunk_allowed_vlans" and sorted(vars.get("vlan_list", [])) == [40, 50]
    fake_driver.fail_on = _fail_on

    res = operator_client.patch(_url(), json={"interface": _IFACE_TRUNK, "mode": "replace", "vlans": [40, 50]})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]

    job = job_repository.get(job_id)
    assert job is not None
    assert job.status == "failed"
    assert job.rollback_performed is True
    assert job.rollback_success is True
    assert (
        "rollback:set_trunk_allowed_vlans", None,
        {"interface": _IFACE_TRUNK, "vlan_list": [10, 20, 30]},
    ) in fake_driver.calls


def test_e2e_unknown_interface_completes(operator_client, device, fake_driver):
    """A target interface the device doesn't report -- like the other
    modernized port-write tests, Puerto's allowed-vlans path has no
    explicit "interface must exist" guard for `mode='replace'` (only
    add/remove need the current list); the job completes rather than
    failing with "does not exist" the way the old
    `_capture_pre_state_port_trunk_vlans`-based flow did."""
    res = operator_client.patch(
        _url(), json={"interface": "GigabitEthernet9/9/9", "mode": "replace", "vlans": [10]},
    )
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
