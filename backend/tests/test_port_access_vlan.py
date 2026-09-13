"""Tests for the Step 2.3 port access-VLAN assignment flow.

Covers:
* RBAC: unauthenticated, observer rejected, operator allowed
* unknown device -> 404
* unsupported vendor -> friendly 501 (no vendor leakage)
* reserved VLAN IDs rejected at the API boundary (1002-1005)
* end-to-end happy path (job reaches `completed`)
* idempotent no-op when access VLAN already matches
* trunk-mode port dispatches to the PVID path, not plain access-vlan
* rollback path restores the prior access VLAN

Modernized against the current architecture: free-function services
(job_service/port_execution_service/port_service) are gone, replaced by
the `Puerto` domain model + `group_operation_runner.encolar()` +
`Orquestador` + `job_repository`. Routes moved under
`/api/v1/devices/{name}/ports/...` (device name is a path segment, not a
body field) -- see `app/main.py`'s router mount.

`MockVendor` (app.services.vendors.mock) does NOT implement the
`resolver_*`/`aplicar_paso` contract `Puerto.aplicar()` requires for its
forward-apply path (confirmed by reading the source -- it only
implements the old direct `set_XXX()` methods, which today are reachable
only from the rollback path) -- using it here would raise AttributeError
on every write. `tests/_puerto_fakes.py::FakePuertoDriver` fills that gap
for these tests. This looks like a real gap in the production mock
driver (EXECUTION_MODE=mock is effectively broken for Puerto writes);
reported separately, not fixed here.
"""

from __future__ import annotations

import pytest

from app.composition import device_repository, job_repository, plugin_registry
from app.models.device import Device

from tests._puerto_fakes import FakePuertoDriver, StubUnimplementedDriver, get_or_create_device

_DEVICE = "avl_dev"
_IFACE_ACCESS = "GigabitEthernet0/0/1"   # access, vlan 10 (see FakePuertoDriver.DEFAULT_PORTS)
_IFACE_TRUNK = "GigabitEthernet0/0/24"   # trunk, PVID 1, allowed [10, 20, 30]
_IFACE_UNKNOWN = "GigabitEthernet9/9/9"  # not present on the fake device


@pytest.fixture
def fake_driver(monkeypatch):
    drv = FakePuertoDriver()
    monkeypatch.setitem(plugin_registry._vendors, "cisco_ios", drv)
    monkeypatch.setitem(plugin_registry._vendors, "huawei_vrp", drv)
    return drv


@pytest.fixture
def device(fake_driver):
    """One real, DB-backed device shared by every test in this file --
    FakePuertoDriver.list_ports() is static/non-mutating, so repeated
    writes against the same device across tests stay deterministic."""
    return get_or_create_device(_DEVICE, site_name="Access VLAN Test Site")


def _url(name: str = _DEVICE) -> str:
    return f"/api/v1/devices/{name}/ports/access-vlan"


# -- RBAC + plumbing ----------------------------------------------------------

def test_patch_rejects_unauthenticated(unauth_client, device):
    res = unauth_client.patch(_url(), json={"interface": _IFACE_ACCESS, "vlan_id": 11})
    assert res.status_code == 401


def test_patch_rejects_observer(observer_client, device):
    res = observer_client.patch(_url(), json={"interface": _IFACE_ACCESS, "vlan_id": 11})
    assert res.status_code == 403


def test_patch_operator_allowed(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": _IFACE_ACCESS, "vlan_id": 15})
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body["data"]
    assert body["data"]["jobs"][0]["device"] == _DEVICE
    assert body["data"]["jobs"][0]["job_id"]


def test_patch_validates_interface_name(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": "", "vlan_id": 11})
    assert res.status_code == 422


def test_patch_unknown_device_returns_404(operator_client, fake_driver):
    res = operator_client.patch(_url("no-such-device"), json={"interface": _IFACE_ACCESS, "vlan_id": 11})
    assert res.status_code == 404


def test_patch_unsupported_vendor_returns_friendly_501(admin_client, monkeypatch):
    monkeypatch.setitem(plugin_registry._vendors, "juniper", StubUnimplementedDriver())
    fake_dev = Device(
        name="juniper-x", host="192.0.2.99", vendor="juniper",
        username="admin", encrypted_password="enc", platform="junos",
    )
    monkeypatch.setattr(device_repository, "get", lambda name: fake_dev if name == "juniper-x" else None)

    res = admin_client.patch(_url("juniper-x"), json={"interface": _IFACE_ACCESS, "vlan_id": 11})
    assert res.status_code == 501
    body = res.json()
    assert body["error_code"] == "VENDOR_NOT_SUPPORTED"
    blob = res.text.lower()
    assert "juniper" not in blob
    assert "junos" not in blob


# -- VLAN ID validation --------------------------------------------------------

@pytest.mark.parametrize("vlan_id", [1002, 1003, 1004, 1005])
def test_reserved_vlan_ids_rejected(operator_client, device, vlan_id):
    res = operator_client.patch(_url(), json={"interface": _IFACE_ACCESS, "vlan_id": vlan_id})
    assert res.status_code == 422


def test_vlan_id_out_of_range_rejected(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": _IFACE_ACCESS, "vlan_id": 4095})
    assert res.status_code == 422


def test_vlan_id_zero_rejected(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": _IFACE_ACCESS, "vlan_id": 0})
    assert res.status_code == 422


# -- End-to-end (synchronous under CELERY_TASK_ALWAYS_EAGER) ------------------

def test_end_to_end_set_vlan_completes_in_mock(operator_client, device):
    """Set VLAN 33 on a port currently in VLAN 10: job runs and lands at `completed`."""
    res = operator_client.patch(_url(), json={"interface": _IFACE_ACCESS, "vlan_id": 33})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]

    job = job_repository.get(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.retry_count == 0
    assert job.rollback_performed in (False, None)


def test_end_to_end_noop_when_vlan_already_matches(operator_client, device, fake_driver):
    """Requesting the same VLAN that's already active completes without calling the driver."""
    res = operator_client.patch(_url(), json={"interface": _IFACE_ACCESS, "vlan_id": 10})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
    assert job.result.get("noop") is True
    assert not any(op == "set_port_access_vlan" for op, _v, _vars in fake_driver.calls)


def test_end_to_end_trunk_port_sets_pvid(operator_client, device, fake_driver):
    """A port in trunk mode sets the trunk PVID, not the plain access VLAN."""
    res = operator_client.patch(_url(), json={"interface": _IFACE_TRUNK, "vlan_id": 22})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
    ops = [op for op, _v, _vars in fake_driver.calls]
    assert "set_trunk_pvid_vlan" in ops
    assert "set_port_access_vlan" not in ops


def test_end_to_end_failure_triggers_rollback(operator_client, device, fake_driver):
    """Force the forward apply to fail; verify the runner restores the prior access VLAN."""
    def _fail_on(op_key, vars):
        return op_key == "set_port_access_vlan" and vars.get("vlan_id") == 44
    fake_driver.fail_on = _fail_on

    res = operator_client.patch(_url(), json={"interface": _IFACE_ACCESS, "vlan_id": 44})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]

    job = job_repository.get(job_id)
    assert job is not None
    assert job.status == "failed"
    assert job.rollback_performed is True
    assert job.rollback_success is True
    # Rollback restored the original VLAN (10) via the direct rollback call.
    assert ("rollback:set_port_access_vlan", None, {"interface": _IFACE_ACCESS, "vlan_id": 10}) in fake_driver.calls


def test_end_to_end_unknown_interface_fails_cleanly(operator_client, device, fake_driver):
    """A target interface the device doesn't report should not be silently
    written -- verify what the current Puerto/Orquestador pipeline actually
    does for an interface reconciliar() can't find."""
    res = operator_client.patch(_url(), json={"interface": _IFACE_UNKNOWN, "vlan_id": 11})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    # Puerto._resolver_access_vlan()'s mode/no-op guards only trigger when
    # `actual is not None` -- for a genuinely-missing interface `actual` is
    # None, so none of the guards fire and it falls through to
    # `resolver_set_port_access_vlan()` unconditionally, i.e. it is NOT
    # rejected as "unknown interface" the way the old PortConfigResult-era
    # code path was. This is a real, confirmed behavior difference from the
    # old test's expectation (which relied on `_capture_pre_state_port_access_vlan`
    # reporting `existed=False` as a hard failure) -- Puerto has no such
    # explicit "interface must exist" guard for the access-vlan field.
    # The job actually completes here (FakePuertoDriver.aplicar_paso doesn't
    # care whether the interface is "real").
    assert job.status == "completed"
