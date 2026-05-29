"""Step 3,4 — Port Management Write API tests.

Validates the full API contract for the three new port write endpoints:

  POST /api/v1/ports/configure
  POST /api/v1/ports/shutdown
  POST /api/v1/ports/enable

Coverage:
* RBAC — unauthenticated (401), observer role (403), operator allowed
* Auth — missing / invalid JWT rejected
* Site scope — operator denied 403 when device is outside allowed sites;
               operator allowed 200 when device is in allowed site;
               admin bypasses site restrictions
* Invalid request — 400/422 for bad inputs (missing fields, bad VLAN IDs,
                    invalid mode combinations)
* Happy path — 200 with correct response shape (success, group_job_id, jobs)
* Mock mode — job created and reaches completed status
* 409 lock conflict — endpoint returns 409 when device is currently locked
* 404 device not found
* 501 unsupported vendor
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.db.models import (
    DeviceModel,
    SiteModel,
    UserAllowedSiteModel,
    UserModel,
)
from app.db.session import get_session
from app.main import app
from app.models.device import Device
from app.schemas.user import UserCreate
from app.services import device_locks, job_service, port_service, user_service


# ── Fixtures & helpers ────────────────────────────────────────────────────────

_DEVICE = "mock_device"
_INTERFACE = "GigabitEthernet0/0/1"


def _client(role: str, username: str | None = None) -> TestClient:
    sub = username or role
    token = create_access_token({"sub": sub, "role": role})
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


@pytest.fixture(autouse=True)
def _clean_sites():
    """Reset site assignments before every test to prevent cross-test pollution."""
    with get_session() as session:
        session.query(UserAllowedSiteModel).delete(synchronize_session=False)
        session.query(SiteModel).delete(synchronize_session=False)
        session.query(DeviceModel).update(
            {DeviceModel.site_id: None}, synchronize_session=False
        )
    yield


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")


@pytest.fixture
def mock_device(monkeypatch):
    dev = Device(
        name=_DEVICE,
        host="192.0.2.1",
        vendor="huawei_vrp",
        username="admin",
        encrypted_password="encrypted",
        platform="vrp",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: dev if name == _DEVICE else None,
    )
    return dev


@pytest.fixture
def stub_pre_state_configure(monkeypatch):
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_configure",
        lambda i, d: {
            "existed": True, "description": "old",
            "admin_up": True, "mode": "access",
            "access_vlan": 10, "allowed_vlans": None,
        },
    )


@pytest.fixture
def stub_pre_state_admin(monkeypatch):
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_shutdown",
        lambda i, d: {"existed": True, "admin_up": True},
    )
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_enable",
        lambda i, d: {"existed": True, "admin_up": False},
    )


def _seed_site(admin_client: TestClient, name: str) -> int:
    r = admin_client.post("/api/v1/sites/", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


def _attach_device_to_site(device_name: str, site_id: int | None) -> None:
    with get_session() as session:
        dev = session.query(DeviceModel).filter_by(name=device_name).first()
        assert dev is not None, f"Device '{device_name}' not in DB"
        dev.site_id = site_id


def _grant_site(username: str, site_ids: list[int]) -> None:
    with get_session() as session:
        row = session.query(UserModel).filter_by(username=username).first()
        assert row is not None
        session.query(UserAllowedSiteModel).filter_by(user_id=row.id).delete(
            synchronize_session=False
        )
        for sid in site_ids:
            session.add(UserAllowedSiteModel(user_id=row.id, site_id=sid))


def _ensure_user(username: str, role: str = "operator") -> None:
    if user_service.get_by_username(username) is None:
        user_service.create_user(
            UserCreate(username=username, password="p@ssword_99", role=role)
        )


# ── 401 — Authentication required ────────────────────────────────────────────

def test_configure_requires_auth():
    res = TestClient(app).post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "x"},
    )
    assert res.status_code == 401


def test_shutdown_requires_auth():
    res = TestClient(app).post(
        "/api/v1/ports/shutdown",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 401


def test_enable_requires_auth():
    res = TestClient(app).post(
        "/api/v1/ports/enable",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 401


def test_configure_invalid_token_rejected():
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer not.a.valid.token"})
    res = c.post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "x"},
    )
    assert res.status_code == 401


# ── 403 — Observer cannot write ───────────────────────────────────────────────

def test_configure_observer_forbidden(mock_mode, mock_device):
    res = _client("observer").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "x"},
    )
    assert res.status_code == 403


def test_shutdown_observer_forbidden(mock_mode, mock_device):
    res = _client("observer").post(
        "/api/v1/ports/shutdown",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 403


def test_enable_observer_forbidden(mock_mode, mock_device):
    res = _client("observer").post(
        "/api/v1/ports/enable",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 403


# ── 403 — Site-scope enforcement ──────────────────────────────────────────────

def test_configure_403_device_outside_allowed_sites(mock_mode):
    _ensure_user("site_op_cfg")
    admin_c = _client("admin")
    site_a = _seed_site(admin_c, "Site-A-cfg")
    site_b = _seed_site(admin_c, "Site-B-cfg")
    _attach_device_to_site(_DEVICE, site_b)
    _grant_site("site_op_cfg", [site_a])

    res = _client("operator", "site_op_cfg").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "x"},
    )
    assert res.status_code == 403


def test_shutdown_403_device_outside_allowed_sites(mock_mode):
    _ensure_user("site_op_sht")
    admin_c = _client("admin")
    site_a = _seed_site(admin_c, "Site-A-sht")
    site_b = _seed_site(admin_c, "Site-B-sht")
    _attach_device_to_site(_DEVICE, site_b)
    _grant_site("site_op_sht", [site_a])

    res = _client("operator", "site_op_sht").post(
        "/api/v1/ports/shutdown",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 403


def test_enable_403_device_outside_allowed_sites(mock_mode):
    _ensure_user("site_op_enb")
    admin_c = _client("admin")
    site_a = _seed_site(admin_c, "Site-A-enb")
    site_b = _seed_site(admin_c, "Site-B-enb")
    _attach_device_to_site(_DEVICE, site_b)
    _grant_site("site_op_enb", [site_a])

    res = _client("operator", "site_op_enb").post(
        "/api/v1/ports/enable",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 403


def test_configure_200_device_in_allowed_site(
    mock_mode, stub_pre_state_configure, monkeypatch
):
    """Operator succeeds when the device is within their allowed site."""
    _ensure_user("site_op_ok")
    admin_c = _client("admin")
    site = _seed_site(admin_c, "Site-OK-cfg")
    _attach_device_to_site(_DEVICE, site)
    _grant_site("site_op_ok", [site])

    dev = Device(
        name=_DEVICE, host="192.0.2.1", vendor="huawei_vrp",
        username="admin", encrypted_password="enc", platform="vrp",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: dev if name == _DEVICE else None,
    )

    res = _client("operator", "site_op_ok").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "allowed"},
    )
    assert res.status_code == 200, res.text


def test_admin_bypasses_site_scope_configure(mock_mode, mock_device, monkeypatch):
    """Admin can configure a device even when it belongs to a different site."""
    admin_c = _client("admin")
    site = _seed_site(admin_c, "Site-Admin-bypass")
    _attach_device_to_site(_DEVICE, site)

    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_configure",
        lambda i, d: {
            "existed": True, "description": None, "admin_up": True,
            "mode": "access", "access_vlan": 1, "allowed_vlans": None,
        },
    )
    res = admin_c.post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "admin ok"},
    )
    assert res.status_code == 200, res.text


# ── 404 — Device not found ────────────────────────────────────────────────────

def test_configure_404_unknown_device(mock_mode, monkeypatch):
    monkeypatch.setattr("app.services.device_service.get_device", lambda name: None)
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": "ghost", "interface": _INTERFACE, "description": "x"},
    )
    assert res.status_code == 404


def test_shutdown_404_unknown_device(mock_mode, monkeypatch):
    monkeypatch.setattr("app.services.device_service.get_device", lambda name: None)
    res = _client("operator").post(
        "/api/v1/ports/shutdown",
        json={"device": "ghost", "interface": _INTERFACE},
    )
    assert res.status_code == 404


def test_enable_404_unknown_device(mock_mode, monkeypatch):
    monkeypatch.setattr("app.services.device_service.get_device", lambda name: None)
    res = _client("operator").post(
        "/api/v1/ports/enable",
        json={"device": "ghost", "interface": _INTERFACE},
    )
    assert res.status_code == 404


# ── 400/422 — Validation errors ───────────────────────────────────────────────

def test_configure_422_no_mutation_fields(mock_mode, mock_device):
    """Schema validator rejects requests with no mutation fields."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 422


def test_configure_422_access_vlan_without_mode(mock_mode, mock_device):
    """access_vlan requires mode='access'."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "access_vlan": 10},
    )
    assert res.status_code == 422


def test_configure_422_allowed_vlans_without_mode(mock_mode, mock_device):
    """allowed_vlans requires mode='trunk'."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "allowed_vlans": [10, 20]},
    )
    assert res.status_code == 422


def test_configure_422_access_vlan_wrong_mode(mock_mode, mock_device):
    """access_vlan=10 with mode='trunk' is invalid."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={
            "device": _DEVICE, "interface": _INTERFACE,
            "mode": "trunk", "access_vlan": 10,
        },
    )
    assert res.status_code == 422


def test_configure_400_invalid_interface_name(mock_mode, mock_device):
    """Malformed interface name returns 400."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": "", "description": "x"},
    )
    # Pydantic rejects empty interface (min_length=2) → 422
    assert res.status_code in (400, 422)


def test_configure_400_reserved_vlan_id(mock_mode, mock_device):
    """VLAN 1003 is in the reserved range and must be rejected."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={
            "device": _DEVICE, "interface": _INTERFACE,
            "mode": "access", "access_vlan": 1003,
        },
    )
    assert res.status_code in (400, 422)


def test_shutdown_422_missing_interface(mock_mode, mock_device):
    res = _client("operator").post(
        "/api/v1/ports/shutdown",
        json={"device": _DEVICE},
    )
    assert res.status_code == 422


def test_enable_422_missing_device(mock_mode, mock_device):
    res = _client("operator").post(
        "/api/v1/ports/enable",
        json={"interface": _INTERFACE},
    )
    assert res.status_code == 422


# ── 409 — Lock conflict ───────────────────────────────────────────────────────

def test_configure_409_device_locked(mock_mode, mock_device, monkeypatch):
    """Returns 409 immediately when the device lock is already held."""
    monkeypatch.setattr(device_locks, "is_device_busy", lambda d: True)
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "x"},
    )
    assert res.status_code == 409
    body = res.json()
    assert body["error_code"] == "DEVICE_LOCKED"


def test_shutdown_409_device_locked(mock_mode, mock_device, monkeypatch):
    """Returns 409 immediately when the device lock is already held."""
    monkeypatch.setattr(device_locks, "is_device_busy", lambda d: True)
    res = _client("operator").post(
        "/api/v1/ports/shutdown",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 409


def test_enable_409_device_locked(mock_mode, mock_device, monkeypatch):
    """Returns 409 immediately when the device lock is already held."""
    monkeypatch.setattr(device_locks, "is_device_busy", lambda d: True)
    res = _client("operator").post(
        "/api/v1/ports/enable",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 409


def test_configure_409_real_lock_held(mock_mode, mock_device, monkeypatch):
    """409 fires when another thread actually holds the device lock."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_configure",
        lambda i, d: {
            "existed": True, "description": "x", "admin_up": True,
            "mode": "access", "access_vlan": 1, "allowed_vlans": None,
        },
    )
    lock_released = threading.Event()

    def _hold_lock():
        with device_locks.acquire(_DEVICE):
            lock_released.wait(timeout=3)

    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()
    time.sleep(0.02)  # let holder acquire the lock

    try:
        res = _client("operator").post(
            "/api/v1/ports/configure",
            json={"device": _DEVICE, "interface": _INTERFACE, "description": "x"},
        )
        assert res.status_code == 409
    finally:
        lock_released.set()
        holder.join(timeout=2)


# ── 501 — Unsupported vendor ──────────────────────────────────────────────────

def test_configure_501_unsupported_vendor(mock_mode, monkeypatch):
    """Returns 501 when the vendor driver has not implemented configure_port."""
    from app.services.vendors.port_driver_base import BasePortDriver

    dev = Device(
        name=_DEVICE, host="192.0.2.1", vendor="stub_vendor",
        username="admin", encrypted_password="enc", platform="stub",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: dev if name == _DEVICE else None,
    )

    class _StubDriver(BasePortDriver):
        def list_ports(self, device, password):
            return []
        # configure_port, shutdown_port, enable_port inherit the NotImplementedError stub

    monkeypatch.setattr(
        "app.services.vendors.dispatcher.get_port_driver",
        lambda device: _StubDriver(),
    )

    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "x"},
    )
    assert res.status_code == 501
    assert res.json()["error_code"] == "VENDOR_NOT_SUPPORTED"


def test_shutdown_501_unsupported_vendor(mock_mode, monkeypatch):
    from app.services.vendors.port_driver_base import BasePortDriver

    dev = Device(
        name=_DEVICE, host="192.0.2.1", vendor="stub_vendor",
        username="admin", encrypted_password="enc", platform="stub",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: dev if name == _DEVICE else None,
    )

    class _StubDriver(BasePortDriver):
        def list_ports(self, device, password):
            return []

    monkeypatch.setattr(
        "app.services.vendors.dispatcher.get_port_driver",
        lambda device: _StubDriver(),
    )

    res = _client("operator").post(
        "/api/v1/ports/shutdown",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 501


def test_enable_501_unsupported_vendor(mock_mode, monkeypatch):
    from app.services.vendors.port_driver_base import BasePortDriver

    dev = Device(
        name=_DEVICE, host="192.0.2.1", vendor="stub_vendor",
        username="admin", encrypted_password="enc", platform="stub",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: dev if name == _DEVICE else None,
    )

    class _StubDriver(BasePortDriver):
        def list_ports(self, device, password):
            return []

    monkeypatch.setattr(
        "app.services.vendors.dispatcher.get_port_driver",
        lambda device: _StubDriver(),
    )

    res = _client("operator").post(
        "/api/v1/ports/enable",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 501


# ── 200 — Happy path & response shape ────────────────────────────────────────

def test_configure_200_response_shape(mock_mode, mock_device, stub_pre_state_configure):
    """POST /configure returns the canonical async-job response envelope."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "happy"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body
    assert isinstance(body["jobs"], list)
    assert len(body["jobs"]) == 1
    job_entry = body["jobs"][0]
    assert job_entry["device"] == _DEVICE
    assert "job_id" in job_entry
    assert job_entry["status"] == "pending"


def test_shutdown_200_response_shape(mock_mode, mock_device, stub_pre_state_admin):
    res = _client("operator").post(
        "/api/v1/ports/shutdown",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body
    assert len(body["jobs"]) == 1
    assert body["jobs"][0]["device"] == _DEVICE


def test_enable_200_response_shape(mock_mode, mock_device, stub_pre_state_admin):
    res = _client("operator").post(
        "/api/v1/ports/enable",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is True
    assert "group_job_id" in body
    assert len(body["jobs"]) == 1


def test_configure_operator_allowed(mock_mode, mock_device, stub_pre_state_configure):
    """Operator role is allowed to call /configure."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "admin_enabled": True},
    )
    assert res.status_code == 200, res.text


def test_configure_admin_allowed(mock_mode, mock_device, stub_pre_state_configure):
    """Admin role is allowed to call /configure."""
    res = _client("admin").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "by admin"},
    )
    assert res.status_code == 200, res.text


# ── Mock mode — job reaches completed ────────────────────────────────────────

def test_configure_mock_job_reaches_completed(
    mock_mode, mock_device, stub_pre_state_configure
):
    """In mock mode the background job completes within a short timeout."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "mock complete"},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]

    time.sleep(0.1)
    job = job_service.get_job(job_id)
    assert job is not None
    assert job.status == "completed"


def test_shutdown_mock_job_reaches_completed(
    mock_mode, mock_device, stub_pre_state_admin
):
    res = _client("operator").post(
        "/api/v1/ports/shutdown",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]

    time.sleep(0.1)
    job = job_service.get_job(job_id)
    assert job.status == "completed"


def test_enable_mock_job_reaches_completed(
    mock_mode, mock_device, stub_pre_state_admin
):
    res = _client("operator").post(
        "/api/v1/ports/enable",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 200
    job_id = res.json()["jobs"][0]["job_id"]

    time.sleep(0.1)
    job = job_service.get_job(job_id)
    assert job.status == "completed"


def test_configure_group_job_created(mock_mode, mock_device, stub_pre_state_configure):
    """Each configure request creates a distinct group job."""
    from app.services import group_job_service

    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "gj-test"},
    )
    assert res.status_code == 200
    gj_id = res.json()["group_job_id"]
    gj = group_job_service.get_group_job(gj_id)
    assert gj is not None
    assert gj.operation == "configure_port"


# ── Multiple configure fields ─────────────────────────────────────────────────

def test_configure_multiple_fields_accepted(
    mock_mode, mock_device, stub_pre_state_configure
):
    """Configure accepts multiple fields in one request."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={
            "device": _DEVICE,
            "interface": _INTERFACE,
            "description": "multi",
            "admin_enabled": True,
        },
    )
    assert res.status_code == 200, res.text


def test_configure_trunk_mode_with_vlans(mock_mode, mock_device, monkeypatch):
    """mode='trunk' with allowed_vlans is accepted and enqueued."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_configure",
        lambda i, d: {
            "existed": True, "description": None, "admin_up": True,
            "mode": "trunk", "access_vlan": 1,
            "allowed_vlans": [10, 20],
        },
    )
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={
            "device": _DEVICE,
            "interface": _INTERFACE,
            "mode": "trunk",
            "allowed_vlans": [10, 20, 30],
        },
    )
    assert res.status_code == 200, res.text


def test_configure_access_mode_with_vlan(
    mock_mode, mock_device, stub_pre_state_configure
):
    """mode='access' with access_vlan is accepted."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={
            "device": _DEVICE,
            "interface": _INTERFACE,
            "mode": "access",
            "access_vlan": 20,
        },
    )
    assert res.status_code == 200, res.text


# ── Lock not held — succeeds ──────────────────────────────────────────────────

def test_configure_succeeds_when_device_free(
    mock_mode, mock_device, stub_pre_state_configure
):
    """Endpoint succeeds (200) when the device is not currently locked."""
    assert not device_locks.is_device_busy(_DEVICE)
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "free"},
    )
    assert res.status_code == 200
