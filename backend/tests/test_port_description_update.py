"""Tests for the Step 2.1 port-description update flow.

Covers:
* validator rejects malformed input (interface name / description)
* PATCH endpoint returns the standard async-job envelope
* DELETE endpoint clears the description
* RBAC: observer rejected, operator allowed
* unsupported vendor returns the friendly 501
* end-to-end (job completes synchronously under CELERY_TASK_ALWAYS_EAGER)
* rollback path verifies prior state is restored
* idempotent no-op when description already matches

Modernized against the current architecture: `app/validators/` (including
`port_validator.py`) is deleted entirely -- the same rules now live as
private functions in `app/models/port.py`
(`_validate_interface_name`/`_validate_description`), exercised directly
below. Routes moved under `/api/v1/devices/{name}/ports/description`
(PATCH sets, DELETE clears); `MockVendor` cannot be used for Puerto
writes (missing `resolver_*`/`aplicar_paso`) -- see
`tests/_puerto_fakes.py::FakePuertoDriver`.
"""

from __future__ import annotations

import pytest

from app.composition import device_repository, job_repository, plugin_registry
from app.models.device import Device
from app.models.port import _validate_description, _validate_interface_name

from tests._puerto_fakes import FakePuertoDriver, StubUnimplementedDriver, get_or_create_device

_DEVICE = "desc_dev"
_IFACE = "GigabitEthernet0/0/1"  # description="Workstation-01" by default


@pytest.fixture
def fake_driver(monkeypatch):
    drv = FakePuertoDriver()
    monkeypatch.setitem(plugin_registry._vendors, "cisco_ios", drv)
    monkeypatch.setitem(plugin_registry._vendors, "huawei_vrp", drv)
    return drv


@pytest.fixture
def device(fake_driver):
    return get_or_create_device(_DEVICE, site_name="Description Update Test Site")


def _url(name: str = _DEVICE) -> str:
    return f"/api/v1/devices/{name}/ports/description"


# -- Validators ---------------------------------------------------------------

def test_validator_accepts_typical_interface_names():
    for name in [
        "Gi0/0/1",
        "GigabitEthernet0/0/1",
        "GigabitEthernet1/0/1",
        "Te1/1",
        "Po1",
        "FastEthernet0/24",
    ]:
        _validate_interface_name(name)


def test_validator_accepts_huawei_speed_prefixed_interface_names():
    """Bug real reportado por el usuario: las interfaces de alta velocidad
    de Huawei nombran la velocidad COMO prefijo -- "10GE1/0/6",
    "25GE1/0/48", "100GE1/0/1" -- a diferencia de GigabitEthernet/
    XGigabitEthernet, no tienen una forma verbosa separada de la que
    abreviarse (no están en _VRP_IFACE_ABBREV a propósito, ya son
    cortas). El regex viejo exigía que el primer carácter fuera una
    letra, rechazando las 3 en la API antes de llegar siquiera al
    device."""
    for name in ["10GE1/0/6", "25GE1/0/48", "100GE1/0/1"]:
        _validate_interface_name(name)


def test_validator_rejects_bad_interface_names():
    for name in ["", " ", "1/0/1", "123", "GigaBitEthernet 0/0/1", "Gi0\n0/1", "x"]:
        with pytest.raises(ValueError):
            _validate_interface_name(name)


def test_validator_accepts_empty_description():
    # Empty description means "clear" -- must NOT raise.
    _validate_description("")


def test_validator_rejects_control_chars_in_description():
    with pytest.raises(ValueError):
        _validate_description("with\nnewline")
    with pytest.raises(ValueError):
        _validate_description("with\ttab")  # \t is 0x09, a control char


def test_validator_rejects_overlength_description():
    with pytest.raises(ValueError):
        _validate_description("x" * 201)


# -- PATCH /description -- RBAC + happy path -----------------------------------

def test_patch_rejects_unauthenticated(unauth_client, device):
    res = unauth_client.patch(_url(), json={"interface": _IFACE, "description": "x"})
    assert res.status_code == 401


def test_patch_rejects_observer(observer_client, device):
    res = observer_client.patch(_url(), json={"interface": _IFACE, "description": "x"})
    assert res.status_code == 403


def test_patch_operator_allowed(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": _IFACE, "description": "user-desk"})
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body["data"]
    assert body["data"]["jobs"][0]["device"] == _DEVICE
    assert body["data"]["jobs"][0]["job_id"]


def test_patch_validates_interface_name(operator_client, device):
    res = operator_client.patch(_url(), json={"interface": "", "description": "x"})
    assert res.status_code == 422


def test_patch_validates_description_length(operator_client, device):
    res = operator_client.patch(
        _url(), json={"interface": _IFACE, "description": "x" * 1000},
    )
    assert res.status_code == 422


def test_patch_unknown_device_returns_404(operator_client, fake_driver):
    res = operator_client.patch(_url("no-such-device"), json={"interface": _IFACE, "description": "x"})
    assert res.status_code == 404


def test_patch_unsupported_vendor_returns_friendly_501(admin_client, monkeypatch):
    monkeypatch.setitem(plugin_registry._vendors, "juniper", StubUnimplementedDriver())
    fake_dev = Device(
        name="juniper-x3", host="192.0.2.99", vendor="juniper",
        username="admin", encrypted_password="enc", platform="junos",
    )
    monkeypatch.setattr(device_repository, "get", lambda name: fake_dev if name == "juniper-x3" else None)

    res = admin_client.patch(_url("juniper-x3"), json={"interface": _IFACE, "description": "x"})
    assert res.status_code == 501
    body = res.json()
    assert body["error_code"] == "VENDOR_NOT_SUPPORTED"
    blob = res.text.lower()
    assert "juniper" not in blob
    assert "junos" not in blob


# -- DELETE /description -- clear ----------------------------------------------

def test_delete_clears_description(operator_client, device, fake_driver):
    res = operator_client.request("DELETE", _url(), json={"interface": _IFACE})
    assert res.status_code == 202, res.text
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
    assert any(op == "update_port_description" and v.get("description") == "" for op, _va, v in fake_driver.calls)


# -- End-to-end -----------------------------------------------------------------

def test_end_to_end_completes_in_mock(operator_client, device):
    """Job reaches `completed`."""
    res = operator_client.patch(_url(), json={"interface": _IFACE, "description": "new-desc"})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]

    job = job_repository.get(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.retry_count == 0
    assert job.rollback_performed in (False, None)


def test_end_to_end_noop_when_description_unchanged(operator_client, device, fake_driver):
    """If the current description matches the requested value, the job
    completes as a no-op without invoking the driver."""
    res = operator_client.patch(_url(), json={"interface": _IFACE, "description": "Workstation-01"})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]

    job = job_repository.get(job_id)
    assert job.status == "completed"
    assert job.result.get("noop") is True
    assert not any(op == "update_port_description" for op, _v, _vars in fake_driver.calls)


def test_end_to_end_failure_triggers_rollback(operator_client, device, fake_driver):
    """Force the driver call to fail; verify the runner triggers rollback
    using the captured pre-state value."""
    def _fail_on(op_key, vars):
        return op_key == "update_port_description" and vars.get("description") == "new-desc"
    fake_driver.fail_on = _fail_on

    res = operator_client.patch(_url(), json={"interface": _IFACE, "description": "new-desc"})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]

    job = job_repository.get(job_id)
    assert job is not None
    assert job.status == "failed"
    assert job.rollback_performed is True
    assert job.rollback_success is True
    # Rollback re-issued the original description ("Workstation-01").
    assert (
        "rollback:update_port_description", None,
        {"interface": _IFACE, "description": "Workstation-01"},
    ) in fake_driver.calls
