import time

from app.services import vlan_service


def test_create_vlan_success(client):
    payload = {"vlan_id": 800, "name": "TEST", "devices": ["mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "jobs" in data
    assert len(data["jobs"]) == 1
    assert "job_id" in data["jobs"][0]
    assert data["jobs"][0]["status"] in ("pending", "running", "completed")


def test_vlan_invalid_id(client):
    payload = {"vlan_id": 5000, "name": "TEST", "devices": ["mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "less_than_equal"


def test_vlan_reserved(client):
    payload = {"vlan_id": 1, "name": "TEST", "devices": ["mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 400
    assert "reserved" in response.text


def test_vlan_name_invalid(client):
    payload = {"vlan_id": 20, "name": "INVALID NAME", "devices": ["mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 400


def test_get_vlans(client):
    response = client.get("/api/v1/vlans/")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert isinstance(data["data"], list)
    assert len(data["data"]) > 0
    assert "vlan_id" in data["data"][0]
    assert "name" in data["data"][0]


def test_delete_vlan_success(client):
    response = client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "jobs" in data
    assert len(data["jobs"]) == 1
    job = data["jobs"][0]
    assert "job_id" in job
    assert job["device"] == "mock_device"


def test_delete_vlan_invalid(client):
    response = client.request("DELETE", "/api/v1/vlans/1", json={"devices": ["mock_device"]})
    assert response.status_code == 400
    assert "reserved" in response.text


def test_update_vlan_reserved_id(client):
    payload = {"description": "Should be blocked", "devices": ["mock_device"]}
    response = client.patch("/api/v1/vlans/1", json=payload)
    assert response.status_code == 400
    assert "reserved" in response.text


def test_update_vlan_reserved_id_range(client):
    payload = {"description": "Should be blocked", "devices": ["mock_device"]}
    response = client.patch("/api/v1/vlans/1002", json=payload)
    assert response.status_code == 400
    assert "reserved" in response.text


def test_update_vlan_description(client):
    payload = {"description": "Core network VLAN", "devices": ["mock_device"]}
    response = client.patch("/api/v1/vlans/10", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "jobs" in data
    assert len(data["jobs"]) == 1
    job = data["jobs"][0]
    assert "job_id" in job
    assert job["device"] == "mock_device"


def test_create_vlan_device_not_found(client):
    payload = {"vlan_id": 50, "name": "TEST", "devices": ["nonexistent_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 404
    assert "nonexistent_device" in response.text


def test_create_vlan_empty_devices_rejected(client):
    payload = {"vlan_id": 50, "name": "TEST", "devices": []}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 422


def test_delete_vlan_failure(client):
    response = client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["fail_device"]})
    assert response.status_code == 200
    # BackgroundTask runs synchronously in TestClient — job is already failed
    job_id = response.json()["jobs"][0]["job_id"]
    response = client.get(f"/api/v1/jobs/{job_id}")
    data = response.json()
    assert data["data"]["status"] == "failed"
    assert data["data"]["error"] is not None


def test_update_vlan_failure(client):
    payload = {"description": "Test desc", "devices": ["fail_device"]}
    response = client.patch("/api/v1/vlans/10", json=payload)
    assert response.status_code == 200
    # BackgroundTask runs synchronously in TestClient — job is already failed
    job_id = response.json()["jobs"][0]["job_id"]
    response = client.get(f"/api/v1/jobs/{job_id}")
    data = response.json()
    assert data["data"]["status"] == "failed"
    assert data["data"]["error"] is not None


def test_delete_nonexistent_vlan_job_fails(client):
    """Deleting a VLAN that doesn't exist on a device marks that job as failed."""
    response = client.request("DELETE", "/api/v1/vlans/99", json={"devices": ["mock_device"]})
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]
    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.json()["data"]["status"] == "failed"


def test_delete_multi_device(client):
    """Delete on multiple devices creates one job per device."""
    response = client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device", "mock_device"]})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert len(data["jobs"]) == 2
    for job in data["jobs"]:
        assert "job_id" in job
        assert job["device"] == "mock_device"


def test_update_multi_device(client):
    """Update on multiple devices creates one job per device."""
    payload = {"description": "Multi update", "devices": ["mock_device", "mock_device"]}
    response = client.patch("/api/v1/vlans/10", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert len(data["jobs"]) == 2
    for job in data["jobs"]:
        assert "job_id" in job
        assert job["device"] == "mock_device"


def test_delete_valid_vlan_still_works(client):
    response = client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "jobs" in data
    assert len(data["jobs"]) == 1


# ── VLAN-003: get_vlans real-mode guard ───────────────────────────────────────

def test_get_vlans_no_device_real_mode_returns_400(client, monkeypatch):
    """GET /vlans/ without ?device= must return 400 in real mode."""
    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")
    response = client.get("/api/v1/vlans/")
    assert response.status_code == 400
    assert "device" in response.json()["error"].lower()


def test_get_vlans_no_device_mock_mode_returns_list(client):
    """GET /vlans/ without ?device= must still return the mock list in mock mode."""
    response = client.get("/api/v1/vlans/")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert isinstance(data["data"], list)
    assert len(data["data"]) > 0


def test_get_vlans_with_device_real_mode(client, monkeypatch):
    """GET /vlans/?device=mock_device passes device to the service; in mock mode it still returns data."""
    captured = {}

    def _fake_get_vlans(device_id=None):
        captured["device_id"] = device_id
        return [{"vlan_id": 10, "name": "MGMT"}]

    monkeypatch.setattr(vlan_service, "get_vlans", _fake_get_vlans)
    response = client.get("/api/v1/vlans/?device=some_device")
    assert response.status_code == 200
    assert captured["device_id"] == "some_device"
