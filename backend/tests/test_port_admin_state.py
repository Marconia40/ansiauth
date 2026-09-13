"""Tests for the Step 2.2 port admin-state (shutdown / no shutdown) flow.

Covers:
* RBAC: unauthenticated, observer rejected, operator allowed
* unknown device -> 404
* unsupported vendor -> friendly 501 (no vendor leakage)
* end-to-end happy path (job reaches `completed`)
* idempotent no-op when admin state already matches
* rollback path restores the prior admin state

Modernized against the current architecture -- see test_port_access_vlan.py
for the detailed rationale (same shape, different field/endpoint):
routes moved under `/api/v1/devices/{name}/ports/admin-state`; writes go
through `Puerto`+`group_operation_runner.encolar()`+`Orquestador`+
`job_repository`; jobs run synchronously under
`CELERY_TASK_ALWAYS_EAGER=True`; write responses are `202` with the body
wrapped as `{"success": true, "data": {"group_job_id", "jobs"}}`;
`ValidationError` -> `422`, not `400`; `MockVendor` cannot be used for
Puerto writes (missing `resolver_*`/`aplicar_paso`) -- see
`tests/_puerto_fakes.py::FakePuertoDriver`, which fills that gap.
"""

from __future__ import annotations

import pytest

from app.composition import device_repository, job_repository, plugin_registry
from app.models.device import Device

from tests._puerto_fakes import FakePuertoDriver, StubUnimplementedDriver, get_or_create_device

_DEVICE = "adm_dev"
_IFACE = "GigabitEthernet0/0/1"  # admin_up=True by default (FakePuertoDriver.DEFAULT_PORTS)


@pytest.fixture
def fake_driver(monkeypatch):
    drv = FakePuertoDriver()
    monkeypatch.setitem(plugin_registry._vendors, "cisco_ios", drv)
    monkeypatch.setitem(plugin_registry._vendors, "huawei_vrp", drv)
    return drv


@pytest.fixture
def device(fake_driver):
    return get_or_create_device(_DEVICE, site_name="Admin State Test Site")


def _url(name: str = _DEVICE) -> str:
    return f"/api/v1/devices/{name}/ports/admin-state"


# -- RBAC + plumbing ----------------------------------------------------------

def test_patch_rejects_unauthenticated(unauth_client, device):
    res = unauth_client.patch(_url(), json={"interface": _IFACE, "enabled": False})
    assert res.status_code == 401


def test_patch_rejects_observer(observer_client, device):
    res = observer_client.patch(_url(), json={"interface": _IFACE, "enabled": False})
    assert res.status_code == 403


def test_patch_operator_allowed(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": _IFACE, "enabled": False})
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body["data"]
    assert body["data"]["jobs"][0]["device"] == _DEVICE
    assert body["data"]["jobs"][0]["job_id"]


def test_patch_validates_interface_name(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": "", "enabled": False})
    assert res.status_code == 422


def test_patch_unknown_device_returns_404(operator_client, fake_driver):
    res = operator_client.patch(_url("no-such-device"), json={"interface": _IFACE, "enabled": False})
    assert res.status_code == 404


def test_patch_unsupported_vendor_returns_friendly_501(admin_client, monkeypatch):
    monkeypatch.setitem(plugin_registry._vendors, "juniper", StubUnimplementedDriver())
    fake_dev = Device(
        name="juniper-x2", host="192.0.2.99", vendor="juniper",
        username="admin", encrypted_password="enc", platform="junos",
    )
    monkeypatch.setattr(device_repository, "get", lambda name: fake_dev if name == "juniper-x2" else None)

    res = admin_client.patch(_url("juniper-x2"), json={"interface": _IFACE, "enabled": False})
    assert res.status_code == 501
    body = res.json()
    assert body["error_code"] == "VENDOR_NOT_SUPPORTED"
    blob = res.text.lower()
    assert "juniper" not in blob
    assert "junos" not in blob


# -- End-to-end (synchronous under CELERY_TASK_ALWAYS_EAGER) ------------------

def test_end_to_end_disable_completes_in_mock(operator_client, device):
    """Disable on a currently-enabled port: job runs and lands at `completed`."""
    res = operator_client.patch(_url(), json={"interface": _IFACE, "enabled": False})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]

    job = job_repository.get(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.retry_count == 0
    assert job.rollback_performed in (False, None)


def test_end_to_end_noop_when_already_in_state(operator_client, device, fake_driver):
    """Asking to enable a port that's already enabled completes without calling the driver."""
    res = operator_client.patch(_url(), json={"interface": _IFACE, "enabled": True})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
    assert job.result.get("noop") is True
    assert not any(op == "set_port_admin_state" for op, _v, _vars in fake_driver.calls)


def test_end_to_end_failure_triggers_rollback(operator_client, device, fake_driver):
    """Force the forward apply to fail; verify the runner restores the prior
    admin state via the rollback path."""
    def _fail_on(op_key, vars):
        return op_key == "set_port_admin_state" and vars.get("enabled") is False
    fake_driver.fail_on = _fail_on

    res = operator_client.patch(_url(), json={"interface": _IFACE, "enabled": False})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]

    job = job_repository.get(job_id)
    assert job is not None
    assert job.status == "failed"
    assert job.rollback_performed is True
    assert job.rollback_success is True
    # Rollback re-issued the original admin state (True) via the direct rollback call.
    assert ("rollback:set_port_admin_state", None, {"interface": _IFACE, "enabled": True}) in fake_driver.calls


def test_end_to_end_unknown_interface_fails_cleanly(operator_client, device, fake_driver):
    """A target interface the device doesn't report -- see the equivalent
    test in test_port_access_vlan.py for the confirmed behavior: Puerto's
    no-op/mode guards only trigger when `actual is not None`, so a missing
    interface is NOT specially rejected here, unlike the old
    `_capture_pre_state_port_admin`-based flow which hard-failed on
    `existed=False`. The job actually completes."""
    res = operator_client.patch(_url(), json={"interface": "GigabitEthernet9/9/9", "enabled": False})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
