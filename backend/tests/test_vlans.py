import time


def test_create_vlan_success(client):
    payload = {"vlan_id": 10, "name": "TEST", "device": "mock_device"}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "job_id" in data["data"]
    assert data["data"]["status"] == "pending"


def test_vlan_invalid_id(client):
    payload = {"vlan_id": 5000, "name": "TEST", "device": "mock_device"}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "less_than_equal"


def test_vlan_reserved(client):
    payload = {"vlan_id": 1, "name": "TEST", "device": "mock_device"}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 400
    assert "reserved" in response.text


def test_vlan_name_invalid(client):
    payload = {"vlan_id": 20, "name": "INVALID NAME", "device": "mock_device"}
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
    response = client.delete("/api/v1/vlans/10?device=mock_device")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "job_id" in data["data"]
    assert data["data"]["status"] == "pending"


def test_delete_vlan_invalid(client):
    response = client.delete("/api/v1/vlans/1?device=mock_device")
    assert response.status_code == 400
    assert "reserved" in response.text


def test_update_vlan_description(client):
    payload = {"description": "Core network VLAN", "device": "mock_device"}
    response = client.patch("/api/v1/vlans/10", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "job_id" in data["data"]
    assert data["data"]["status"] == "pending"


def test_create_vlan_device_not_found(client):
    payload = {"vlan_id": 50, "name": "TEST", "device": "nonexistent_device"}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 404
    assert "nonexistent_device" in response.text


def test_delete_vlan_failure(client):
    response = client.delete("/api/v1/vlans/10?device=fail_device")
    assert response.status_code == 200
    job_id = response.json()["data"]["job_id"]

    time.sleep(1)

    response = client.get(f"/api/v1/jobs/{job_id}")
    data = response.json()
    assert data["data"]["status"] == "failed"
    assert data["data"]["error"] is not None


def test_update_vlan_failure(client):
    payload = {"description": "Test desc", "device": "fail_device"}
    response = client.patch("/api/v1/vlans/10", json=payload)
    assert response.status_code == 200
    job_id = response.json()["data"]["job_id"]

    time.sleep(1)

    response = client.get(f"/api/v1/jobs/{job_id}")
    data = response.json()
    assert data["data"]["status"] == "failed"
    assert data["data"]["error"] is not None
