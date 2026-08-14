"""Tests for INV-001 — PUT /api/v1/devices/{name}."""
import pytest

from app.db.models import AuditLogModel
from app.db.session import get_session
from app.services import audit_service, device_service


@pytest.fixture(autouse=True)
def reset_devices():
    device_service.clear_devices()
    yield
    device_service.clear_devices()


@pytest.fixture(autouse=True)
def clear_audit():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


_BASE = {
    "name": "router1",
    "host": "192.168.10.1",
    "vendor": "cisco_ios",
    "platform": "ios",
    "username": "admin",
    "password": "secret",
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
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"vendor": "huawei"})
    assert resp.status_code == 200
    assert resp.json()["data"]["vendor"] == "huawei"


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
    from app.services import secret_service
    device = device_service.get_device(router1)
    assert secret_service.decrypt_password(device.encrypted_password) == "newpassword"


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


def test_invalid_vendor_returns_400(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={"vendor": "juniper"})
    assert resp.status_code == 400


def test_empty_payload_returns_400(admin_client, router1):
    resp = admin_client.put(f"/api/v1/devices/{router1}", json={})
    assert resp.status_code == 400


def test_update_nonexistent_device_returns_400(admin_client):
    resp = admin_client.put("/api/v1/devices/ghost", json={"host": "1.2.3.4"})
    assert resp.status_code == 400


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
