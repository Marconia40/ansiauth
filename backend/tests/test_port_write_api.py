"""Port Management Write API tests -- RBAC, site scope, 501, response
shape, across the granular endpoints that replace the old unified
`POST /api/v1/ports/configure`.

Modernized against the current architecture:
* Routes moved to `/api/v1/devices/{name}/ports/...` (device is a path
  segment, not a body field).
* The old unified `/configure` endpoint (arbitrary multi-field combos)
  is gone. `shutdown`/`enable` map 1:1 to
  `POST .../ports/shutdown`/`POST .../ports/enable`. Multi-field combos
  (e.g. description + admin_up together) map to
  `POST .../ports/batch` (one job, one SSH connection, N `Puerto`
  entries). Atomic mode+VLAN combos map to the dedicated
  `POST .../ports/access-mode` / `POST .../ports/trunk-mode` endpoints.
* `device_locks`/409-lock-conflict tests have NO modern equivalent --
  confirmed by reading `app/api/ports.py` end to end: there is no
  API-layer lock precheck any more. Locking now happens INSIDE
  `Orquestador.ejecutar()` via `RedisCoordinator.bloquear()`, which
  BLOCKS AND WAITS for the lock instead of rejecting up front, and
  `CELERY_TASK_ALWAYS_EAGER=True` makes the whole job run synchronously
  inside the request anyway -- there is no window in which a 2nd request
  could observe "device busy". Dropped entirely, not ported.
* `user_service` -> `app.composition.user_repository`.
* `MockVendor` cannot be used for Puerto writes (missing
  `resolver_*`/`aplicar_paso`) -- see
  `tests/_puerto_fakes.py::FakePuertoDriver`.
* Write responses are `202` with body `{"success": true, "data":
  {"group_job_id", "jobs"}}`; `ValidationError` -> `422`, not `400`.
"""

from __future__ import annotations

import pytest

from app.composition import device_repository, job_repository, plugin_registry, user_repository
from app.db.models import DeviceGroupModel, DeviceModel, RoleAssignmentModel, SiteModel, UserModel
from app.db.session import get_session
from app.models.device import Device

from tests._puerto_fakes import FakePuertoDriver, StubUnimplementedDriver, get_or_create_device, get_or_create_site

_DEVICE = "wr_dev"
_IFACE = "GigabitEthernet0/0/1"


@pytest.fixture
def fake_driver(monkeypatch):
    drv = FakePuertoDriver()
    monkeypatch.setitem(plugin_registry._vendors, "cisco_ios", drv)
    monkeypatch.setitem(plugin_registry._vendors, "huawei_vrp", drv)
    return drv


@pytest.fixture
def device(fake_driver):
    return get_or_create_device(_DEVICE, site_name="Port Write API Test Site")


def _u(path: str, name: str = _DEVICE) -> str:
    return f"/api/v1/devices/{name}/ports/{path}"


# -- Site-scope helpers (same pattern as tests/test_group_job.py) ------------

_TEST_USER_ROLES: dict[str, str] = {}


def _ensure_user(username: str, role: str = "operator") -> None:
    _TEST_USER_ROLES[username] = role
    if user_repository.obtener_por_username(username) is None:
        is_sys = role in {"admin", "super-admin"}
        user_repository.crear(username, "p@ssword_99", is_system_admin=is_sys)


def _grant_site(username: str, site_ids: list[int]) -> None:
    role = _TEST_USER_ROLES.get(username, "operator")
    with get_session() as session:
        row = session.query(UserModel).filter_by(username=username).first()
        assert row is not None
        session.query(RoleAssignmentModel).filter(
            RoleAssignmentModel.user_id == row.id,
            RoleAssignmentModel.device_group_id.is_(None),
        ).delete(synchronize_session=False)
        for sid in site_ids:
            session.add(RoleAssignmentModel(user_id=row.id, site_id=sid, device_group_id=None, role=role))


def _attach_device_to_site(device_name: str, site_id: int) -> None:
    with get_session() as session:
        dev = session.query(DeviceModel).filter_by(name=device_name).first()
        assert dev is not None, f"Device '{device_name}' not in DB"
        row = session.query(SiteModel).filter_by(id=site_id).first()
        assert row is not None and row.default_group_id is not None
        dev.device_group_id = row.default_group_id


def _client(role: str, username: str | None = None):
    from app.core.security import create_access_token
    from fastapi.testclient import TestClient
    from app.main import app

    sub = username or role
    is_system_admin = role in {"admin", "super-admin"}
    token = create_access_token({"sub": sub, "is_system_admin": is_system_admin})
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


# -- 401 -- authentication required -------------------------------------------

def test_shutdown_requires_auth(unauth_client, device):
    res = unauth_client.post(_u("shutdown"), json={"interface": _IFACE})
    assert res.status_code == 401


def test_enable_requires_auth(unauth_client, device):
    res = unauth_client.post(_u("enable"), json={"interface": _IFACE})
    assert res.status_code == 401


def test_batch_requires_auth(unauth_client, device):
    res = unauth_client.post(_u("batch"), json={"changes": [{"interface": _IFACE, "description": "x"}]})
    assert res.status_code == 401


def test_invalid_token_rejected(device):
    c = _client("operator")
    c.headers.update({"Authorization": "Bearer not.a.valid.token"})
    res = c.post(_u("shutdown"), json={"interface": _IFACE})
    assert res.status_code == 401


# -- 403 -- observer cannot write ---------------------------------------------

def test_shutdown_observer_forbidden(observer_client, device):
    res = observer_client.post(_u("shutdown"), json={"interface": _IFACE})
    assert res.status_code == 403


def test_enable_observer_forbidden(observer_client, device):
    res = observer_client.post(_u("enable"), json={"interface": _IFACE})
    assert res.status_code == 403


def test_batch_observer_forbidden(observer_client, device):
    res = observer_client.post(_u("batch"), json={"changes": [{"interface": _IFACE, "description": "x"}]})
    assert res.status_code == 403


# -- 403 -- site-scope enforcement --------------------------------------------

def test_shutdown_403_device_outside_allowed_sites(fake_driver):
    _ensure_user("site_op_sht")
    site_a = get_or_create_site("Site-A-sht")
    site_b = get_or_create_site("Site-B-sht")
    dev = get_or_create_device("wr_dev_sht", site_name="Site-B-sht")
    _attach_device_to_site(dev.name, site_b.id)
    _grant_site("site_op_sht", [site_a.id])

    res = _client("operator", "site_op_sht").post(_u("shutdown", dev.name), json={"interface": _IFACE})
    assert res.status_code == 403


def test_shutdown_200_device_in_allowed_site(fake_driver):
    _ensure_user("site_op_ok")
    site = get_or_create_site("Site-OK-sht")
    dev = get_or_create_device("wr_dev_ok", site_name="Site-OK-sht")
    _attach_device_to_site(dev.name, site.id)
    _grant_site("site_op_ok", [site.id])

    res = _client("operator", "site_op_ok").post(_u("shutdown", dev.name), json={"interface": _IFACE})
    assert res.status_code == 202, res.text


def test_admin_bypasses_site_scope(fake_driver):
    site = get_or_create_site("Site-Admin-bypass-wr")
    dev = get_or_create_device("wr_dev_admin_bypass", site_name="Site-Admin-bypass-wr")
    _attach_device_to_site(dev.name, site.id)
    # No grant given to the admin caller -- is_system_admin bypasses scope.
    res = _client("admin").post(_u("shutdown", dev.name), json={"interface": _IFACE})
    assert res.status_code == 202, res.text


# -- 404 -- device not found --------------------------------------------------

def test_shutdown_404_unknown_device(operator_client, fake_driver):
    res = operator_client.post(_u("shutdown", "ghost-device"), json={"interface": _IFACE})
    assert res.status_code == 404


def test_enable_404_unknown_device(operator_client, fake_driver):
    res = operator_client.post(_u("enable", "ghost-device"), json={"interface": _IFACE})
    assert res.status_code == 404


# -- 422 -- validation errors -------------------------------------------------

def test_shutdown_422_missing_interface(operator_client, device):
    res = operator_client.post(_u("shutdown"), json={})
    assert res.status_code == 422


def test_batch_422_no_changes(operator_client, device):
    res = operator_client.post(_u("batch"), json={"changes": []})
    assert res.status_code == 422


def test_batch_422_entry_with_no_fields(operator_client, device):
    """A batch entry with only `interface` and no mutation field is
    rejected -- `expandir_a_puertos()` raises ValueError, mapped to 422."""
    res = operator_client.post(_u("batch"), json={"changes": [{"interface": _IFACE}]})
    assert res.status_code == 422


def test_access_mode_400_reserved_vlan(operator_client, device):
    res = operator_client.post(_u("access-mode"), json={"interface": _IFACE, "access_vlan": 1003})
    assert res.status_code == 422


# -- 501 -- unsupported vendor -------------------------------------------------

def test_shutdown_501_unsupported_vendor(admin_client, monkeypatch):
    monkeypatch.setitem(plugin_registry._vendors, "juniper", StubUnimplementedDriver())
    fake_dev = Device(
        name="juniper-wr", host="192.0.2.99", vendor="juniper",
        username="admin", encrypted_password="enc", platform="junos",
    )
    monkeypatch.setattr(device_repository, "get", lambda name: fake_dev if name == "juniper-wr" else None)

    res = admin_client.post(_u("shutdown", "juniper-wr"), json={"interface": _IFACE})
    assert res.status_code == 501
    assert res.json()["error_code"] == "VENDOR_NOT_SUPPORTED"


def test_enable_501_unsupported_vendor(admin_client, monkeypatch):
    monkeypatch.setitem(plugin_registry._vendors, "juniper2", StubUnimplementedDriver())
    fake_dev = Device(
        name="juniper-wr2", host="192.0.2.99", vendor="juniper2",
        username="admin", encrypted_password="enc", platform="junos",
    )
    monkeypatch.setattr(device_repository, "get", lambda name: fake_dev if name == "juniper-wr2" else None)

    res = admin_client.post(_u("enable", "juniper-wr2"), json={"interface": _IFACE})
    assert res.status_code == 501


# -- 200/202 -- happy path & response shape -----------------------------------

def test_shutdown_response_shape(operator_client, device):
    res = operator_client.post(_u("shutdown"), json={"interface": _IFACE})
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body["data"]
    assert len(body["data"]["jobs"]) == 1
    assert body["data"]["jobs"][0]["device"] == _DEVICE


def test_enable_response_shape(operator_client, device):
    res = operator_client.post(_u("enable"), json={"interface": _IFACE})
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body["data"]
    assert len(body["data"]["jobs"]) == 1


def test_admin_allowed(admin_client, device):
    res = admin_client.post(_u("shutdown"), json={"interface": _IFACE})
    assert res.status_code == 202, res.text


# -- Mock mode -- job reaches completed ---------------------------------------

def test_shutdown_job_reaches_completed(operator_client, device):
    res = operator_client.post(_u("shutdown"), json={"interface": _IFACE})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"


def test_enable_job_reaches_completed(operator_client, device, fake_driver):
    """GigabitEthernet0/0/1 is admin_up=True by default -- enabling it is a no-op."""
    res = operator_client.post(_u("enable"), json={"interface": _IFACE})
    assert res.status_code == 202
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
    assert job.result.get("noop") is True


# -- Multi-field combos -- /batch and the atomic mode endpoints ---------------

def test_batch_multiple_fields_one_interface(operator_client, device, fake_driver):
    """The modern equivalent of the old composite `/configure` request:
    one `/batch` call carrying several fields for the same interface, in
    a single job/connection."""
    res = operator_client.post(
        _u("batch"),
        json={"changes": [{"interface": _IFACE, "description": "multi", "admin_up": False}]},
    )
    assert res.status_code == 202, res.text
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    assert len(res.json()["data"]["jobs"]) == 1
    job = job_repository.get(job_id)
    assert job.status == "completed"
    ops = [op for op, _v, _vars in fake_driver.calls]
    assert "update_port_description" in ops
    assert "set_port_admin_state" in ops


def test_access_mode_with_vlan(operator_client, device, fake_driver):
    """mode='access' + access_vlan, atomically -- the modern successor of
    the old `/configure` mode+vlan combo."""
    res = operator_client.post(_u("access-mode"), json={"interface": _IFACE, "access_vlan": 20})
    assert res.status_code == 202, res.text
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
    assert any(op == "set_access_mode" for op, _v, _vars in fake_driver.calls)


def test_trunk_mode_with_vlans(operator_client, device, fake_driver):
    """mode='trunk' + native_vlan + allowed_vlans, atomically."""
    res = operator_client.post(
        _u("trunk-mode"), json={"interface": _IFACE, "native_vlan": 1, "allowed_vlans": [10, 20, 30]},
    )
    assert res.status_code == 202, res.text
    job_id = res.json()["data"]["jobs"][0]["job_id"]
    job = job_repository.get(job_id)
    assert job.status == "completed"
    assert any(op == "set_trunk_mode" for op, _v, _vars in fake_driver.calls)
