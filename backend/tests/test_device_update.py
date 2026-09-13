"""Tests for INV-001 — PUT /api/v1/devices/{name}."""
import pytest

from app.db.models import AuditLogModel, DeviceModel
from app.db.session import get_session


_TEST_DEVICE_NAMES = ("router1",)


@pytest.fixture(autouse=True)
def reset_devices():
    """Clean up only the device names THIS file creates -- a blanket
    DeviceModel.delete() here was found (running the full suite) to also
    wipe shared fixture devices other files register once at collection
    time (mock_device/fail_device/etc.), breaking whichever of those files
    happened to run afterward in the same session."""
    def _clear():
        with get_session() as session:
            session.query(DeviceModel).filter(
                DeviceModel.name.in_(_TEST_DEVICE_NAMES)
            ).delete(synchronize_session=False)

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def clear_audit():
    def _clear():
        with get_session() as session:
            session.query(AuditLogModel).delete(synchronize_session=False)

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _force_mock_vendor_drivers(monkeypatch):
    """See tests/test_devices.py for why this is needed: this backend's real
    .env sets EXECUTION_MODE=real, so app.composition.plugin_registry holds
    the real Cisco/Huawei drivers rather than MockVendor. Registering/moving
    a device fires a fire-and-forget sync task that would otherwise hit the
    real driver's read path, which conftest's simplistic ansible mock can't
    satisfy."""
    from app.composition import plugin_registry
    from app.services.vendors.mock import MockVendor

    mock = MockVendor()
    for vendor in ("cisco_ios", "huawei_vrp"):
        monkeypatch.setitem(plugin_registry._vendors, vendor, mock)


_BASE = {
    "name": "router1",
    "host": "192.168.10.1",
    "vendor": "cisco_ios",
    "platform": "ios",
    "username": "admin",
    "password": "secret",
    # MSP: Phase 4 — DeviceCreate.site_id required (Base-Infra id=1).
    "site_id": 1,
}


@pytest.fixture()
def router1(admin_client):
    admin_client.post("/api/v1/devices/", json=_BASE)
    return "router1"


# ── Happy path ────────────────────────────────────────────────────────────────

def test_update_device_host(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"host": "10.0.0.1"})
    assert resp.status_code == 200
    assert resp.json()["data"]["host"] == "10.0.0.1"


def test_update_device_username(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"username": "netops"})
    assert resp.status_code == 200
    assert resp.json()["data"]["username"] == "netops"


def test_update_device_vendor(admin_client, router1):
    # _VALID_VENDORS is now exactly {"cisco_ios", "huawei_vrp"} (app/models/
    # device.py) -- "huawei" (the legacy free-text value) is no longer
    # accepted, use a valid vendor for the happy-path case.
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"vendor": "huawei_vrp"})
    assert resp.status_code == 200
    assert resp.json()["data"]["vendor"] == "huawei_vrp"


def test_update_device_platform(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"platform": "nxos"})
    assert resp.status_code == 200
    assert resp.json()["data"]["platform"] == "nxos"


def test_partial_update_only_changes_given_fields(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"host": "10.0.0.2"})
    data = resp.json()["data"]
    assert data["host"] == "10.0.0.2"
    assert data["vendor"] == "cisco_ios"   # unchanged
    assert data["username"] == "admin"     # unchanged
    assert data["platform"] == "ios"       # unchanged


def test_update_device_password_re_encrypts(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"password": "newpassword"})
    assert resp.status_code == 200
    from app.composition import device_repository, secret_vault
    device = device_repository.get(router1)
    assert secret_vault.decrypt(device.encrypted_password) == "newpassword"


def test_update_device_response_excludes_password(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"username": "ops"})
    assert "password" not in resp.json()["data"]
    assert "encrypted_password" not in resp.json()["data"]


def test_update_with_hostname(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"host": "router.example.com"})
    assert resp.status_code == 200
    assert resp.json()["data"]["host"] == "router.example.com"


def test_update_with_ipv6(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"host": "2001:db8::1"})
    assert resp.status_code == 200
    assert resp.json()["data"]["host"] == "2001:db8::1"


# ── Audit log ─────────────────────────────────────────────────────────────────

def test_update_writes_audit_log(admin_client, router1):
    admin_client.put(f"/api/v1/devices/{router1}", json={"host": "10.1.1.1"})
    with get_session() as session:
        entry = session.query(AuditLogModel).filter_by(action="update_device").first()
        assert entry is not None
        assert entry.details["name"] == router1
        assert entry.details["updated_fields"]["host"] == "10.1.1.1"


def test_password_not_in_audit_log(admin_client, router1):
    admin_client.put(f"/api/v1/devices/{router1}", json={"password": "supersecret"})
    with get_session() as session:
        entry = session.query(AuditLogModel).filter_by(action="update_device").first()
        assert "password" not in entry.details.get("updated_fields", {})


# ── Validation ────────────────────────────────────────────────────────────────

def test_invalid_ip_returns_422(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"host": "not an ip"})
    assert resp.status_code == 422


def test_invalid_ip_with_bad_chars_returns_422(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"host": "999.999.999.999"})
    assert resp.status_code == 422


def test_invalid_vendor_returns_422(admin_client, router1):
    # app/main.py's ValidationError handler now maps to 422, not 400
    # (verified across the whole app).
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"vendor": "juniper"})
    assert resp.status_code == 422


def test_empty_payload_returns_422(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={})
    assert resp.status_code == 422


def test_update_nonexistent_device_returns_403(admin_client):
    # require_scope("edit_device") (app/core/scope.py) runs as a
    # pre-handler FastAPI dependency: for a device name that doesn't exist
    # it can't resolve (site_id, device_group_id), so `role` stays None and
    # _enforce() 403s before update_device()'s own NotFoundError check ever
    # runs -- same as DELETE /devices/{name} (see test_devices.py).
    resp = admin_client.put("/api/v1/devices/ghost", json={"host": "1.2.3.4"})
    assert resp.status_code == 403


# ── Access control ────────────────────────────────────────────────────────────

def test_operator_cannot_update_device(operator_client, router1):
    resp = operator_client.put(f"/api/v1/devices/{router1}", json={"host": "1.2.3.4"})
    assert resp.status_code == 403


def test_observer_cannot_update_device(observer_client, router1):
    resp = observer_client.put(f"/api/v1/devices/{router1}", json={"host": "1.2.3.4"})
    assert resp.status_code == 403


def test_unauthenticated_cannot_update_device(unauth_client, router1):
    resp = unauth_client.put(f"/api/v1/devices/{router1}", json={"host": "1.2.3.4"})
    assert resp.status_code == 401
