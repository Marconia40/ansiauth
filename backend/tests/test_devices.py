import pytest

from app.db.models import AuditLogModel, DeviceModel
from app.db.session import get_session


_TEST_DEVICE_NAMES = ("switch1", "sw2")


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
def _force_mock_vendor_drivers(monkeypatch):
    """This backend's real .env sets EXECUTION_MODE=real, so
    app.composition.plugin_registry (built once at process import) holds the
    REAL Cisco/Huawei drivers, not MockVendor. The real drivers expect a
    realistic multi-command SSH transcript from ansible_service.run_playbook;
    conftest's mock_ansible_service fixture only returns one generic stdout
    string, which is enough for VLAN mutation ops but not for the read/
    reconciliation paths (get_svis/read_core_state) real drivers exercise on
    every write. Swap in MockVendor for both vendor keys for the duration of
    each test -- same driver instance code path the app itself uses when
    EXECUTION_MODE really is "mock" (see app/composition.py:build_plugin_registry).
    """
    from app.composition import plugin_registry
    from app.services.vendors.mock import MockVendor

    mock = MockVendor()
    for vendor in ("cisco_ios", "huawei_vrp"):
        monkeypatch.setitem(plugin_registry._vendors, vendor, mock)


def _clear_audit_log():
    with get_session() as session:
        session.query(AuditLogModel).delete(synchronize_session=False)


_PAYLOAD = {
    "name": "switch1",
    "host": "192.168.1.10",
    "vendor": "cisco_ios",
    "username": "admin",
    "password": "admin",
    # MSP: Phase 4 — DeviceCreate.site_id is required. Base-Infrastructure is
    # id=1 by bootstrap convention and admin_client is a system-admin so has
    # visibility.
    "site_id": 1,
}


def test_create_device(admin_client):
    response = admin_client.post("/api/v1/devices/", json=_PAYLOAD)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["name"] == "switch1"
    assert data["data"]["host"] == "192.168.1.10"
    assert "password" not in data["data"]
    assert "encrypted_password" not in data["data"]


def test_create_device_no_plaintext_password(admin_client):
    response = admin_client.post("/api/v1/devices/", json=_PAYLOAD)
    body = response.text
    assert "admin" not in body or '"username": "admin"' not in body or "password" not in body
    # Verify no plaintext password field is ever returned
    assert "encrypted_password" not in body
    assert response.json()["data"].get("password") is None


def test_get_devices(admin_client):
    response = admin_client.get("/api/v1/devices/")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert isinstance(data["data"], list)
    for device in data["data"]:
        assert "password" not in device
        assert "encrypted_password" not in device


def test_get_device_by_name(admin_client):
    admin_client.post("/api/v1/devices/", json=_PAYLOAD)
    response = admin_client.get("/api/v1/devices/switch1")
    assert response.status_code == 200
    data = response.json()
    assert data["data"]["name"] == "switch1"
    assert "password" not in data["data"]


def test_get_device_not_found(admin_client):
    response = admin_client.get("/api/v1/devices/nonexistent")
    assert response.status_code == 404


def test_delete_device(admin_client):
    admin_client.post("/api/v1/devices/", json=_PAYLOAD)
    response = admin_client.delete("/api/v1/devices/switch1")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["name"] == "switch1"


def test_delete_device_not_found(admin_client):
    response = admin_client.delete("/api/v1/devices/nonexistent")
    # require_scope("delete_device") now runs as a pre-handler FastAPI
    # dependency (app/core/scope.py) -- for a device that doesn't exist it
    # can't resolve (site_id, device_group_id), so `role` stays None and
    # _enforce() 403s before Inventory.deregister()'s own 404 check ever
    # runs (this holds even for a system-admin caller, since the None-role
    # short-circuit happens before `scope.rol_para()`'s super-admin check).
    assert response.status_code == 403
    assert "nonexistent" in response.text


def test_observer_cannot_create_device(observer_client):
    response = observer_client.post("/api/v1/devices/", json=_PAYLOAD)
    assert response.status_code == 403


def test_create_vlan_with_registered_device(admin_client):
    admin_client.post("/api/v1/devices/", json=_PAYLOAD)
    payload = {"vlan_id": 50, "name": "PROD", "devices": ["switch1"]}
    response = admin_client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 202  # POST /vlans/ is now async (202 Accepted)
    assert response.json()["data"]["jobs"][0]["status"] in ("pending", "running", "completed")


def test_create_vlan_device_not_registered(admin_client):
    payload = {"vlan_id": 50, "name": "PROD", "devices": ["unknown_switch"]}
    response = admin_client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 404
    assert "unknown_switch" in response.text


def test_device_creation_is_audited(admin_client):
    """MSP: Phase 4 — device create routes through ``Inventory.register``,
    which emits a DomainEvent with tipo='device_registrado' (was
    action='create_device' in the legacy path; payload never carried
    "accion" for this event so AuditRecord.desde() falls back to the raw
    event type, not a friendlier "register_device")."""
    _clear_audit_log()
    admin_client.post("/api/v1/devices/", json={**_PAYLOAD, "name": "sw2"})
    log = admin_client.get("/api/v1/audit/").json()["data"]["items"]
    entry = next(e for e in log if e["action"] == "device_registrado")
    assert entry["resource"] == "device"
    # The device_registrado payload only carries {"site_id": ...} -- the
    # device name lives on AuditRecord.device (populated from the event's
    # `device` argument), not in `details`.
    assert entry["device"] == "sw2"
    _clear_audit_log()


def test_password_encryption(admin_client):
    """Password stored in memory must not be plaintext."""
    from app.composition import device_repository, secret_vault

    admin_client.post("/api/v1/devices/", json=_PAYLOAD)
    device = device_repository.get("switch1")
    assert device is not None
    assert device.encrypted_password != "admin"
    assert secret_vault.decrypt(device.encrypted_password) == "admin"
