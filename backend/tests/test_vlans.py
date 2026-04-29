def test_create_vlan_success(client):
    payload = {
        "vlan_id": 10,
        "name": "TEST",
        "device": "mock_device"
    }

    response = client.post("/api/v1/vlans/", json=payload)

    assert response.status_code == 200
    data = response.json()

    assert "job_id" in data
    assert data["status"] == "pending"


def test_vlan_invalid_id(client):
    payload = {
        "vlan_id": 5000,
        "name": "TEST",
        "device": "mock_device"
    }

    response = client.post("/api/v1/vlans/", json=payload)

    assert response.status_code == 422
    assert "less than or equal" in response.text


def test_vlan_reserved(client):
    payload = {
        "vlan_id": 1,
        "name": "TEST",
        "device": "mock_device"
    }

    response = client.post("/api/v1/vlans/", json=payload)

    assert response.status_code == 400
    assert "reserved" in response.text


def test_vlan_name_invalid(client):
    payload = {
        "vlan_id": 20,
        "name": "INVALID NAME",
        "device": "mock_device"
    }

    response = client.post("/api/v1/vlans/", json=payload)

    assert response.status_code == 400
