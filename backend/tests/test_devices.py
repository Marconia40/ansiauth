import pytest

from app.models.device import Device
from app.services import device_service


@pytest.fixture(autouse=True)
def reset_devices():
    device_service.clear_devices()
    yield
    device_service.clear_devices()


def test_create_device(admin_client):
    payload = {"id": "switch1", "ip": "192.168.1.10", "type": "cisco", "username": "admin", "password": "admin"}
    response = admin_client.post("/api/v1/devices/", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["id"] == "switch1"
    assert data["data"]["ip"] == "192.168.1.10"


def test_get_devices(admin_client):
    response = admin_client.get("/api/v1/devices/")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert isinstance(data["data"], list)


def test_delete_device(admin_client):
    admin_client.post("/api/v1/devices/", json={"id": "switch1", "ip": "192.168.1.10", "type": "cisco", "username": "admin", "password": "admin"})
    response = admin_client.delete("/api/v1/devices/switch1")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["id"] == "switch1"


def test_delete_device_not_found(admin_client):
    response = admin_client.delete("/api/v1/devices/nonexistent")
    assert response.status_code == 404
    assert "nonexistent" in response.text


def test_observer_cannot_create_device(observer_client):
    payload = {"id": "switch1", "ip": "192.168.1.10", "type": "cisco", "username": "admin", "password": "admin"}
    response = observer_client.post("/api/v1/devices/", json=payload)
    assert response.status_code == 403


def test_create_vlan_with_registered_device(admin_client):
    device_service.create_device(Device(id="switch1", ip="192.168.1.10", type="cisco", username="admin", password="admin"))
    payload = {"vlan_id": 50, "name": "PROD", "device": "switch1"}
    response = admin_client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "pending"


def test_create_vlan_device_not_registered(admin_client):
    payload = {"vlan_id": 50, "name": "PROD", "device": "unknown_switch"}
    response = admin_client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 404
    assert "unknown_switch" in response.text


def test_device_creation_is_audited(admin_client):
    from app.services import audit_service
    audit_service.clear_audit_log()
    admin_client.post("/api/v1/devices/", json={"id": "sw2", "ip": "10.0.0.1", "type": "cisco", "username": "admin", "password": "admin"})
    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "create_device")
    assert entry["resource"] == "device"
    assert entry["details"]["id"] == "sw2"
    audit_service.clear_audit_log()
