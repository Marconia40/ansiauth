import time


def test_multi_device_vlan(client):
    """Multi-device request creates one independent job per device."""
    payload = {"vlan_id": 100, "name": "MULTI_TEST", "devices": ["mock_device", "mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "jobs" in data
    assert len(data["jobs"]) == 2
    for entry in data["jobs"]:
        assert "device" in entry
        assert "job_id" in entry
        assert "status" in entry
        assert entry["device"] == "mock_device"


def test_one_device_fails(client):
    """When one device fails the other job still completes successfully."""
    payload = {
        "vlan_id": 101,
        "name": "ONETESTFAIL",
        "devices": ["mock_device", "fail_device"],
    }
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    jobs = {entry["device"]: entry["job_id"] for entry in data["jobs"]}

    time.sleep(1)

    resp = client.get(f"/api/v1/jobs/{jobs['mock_device']}")
    assert resp.json()["data"]["status"] == "completed"

    resp = client.get(f"/api/v1/jobs/{jobs['fail_device']}")
    result = resp.json()["data"]
    assert result["status"] == "failed"
    assert result["error"] is not None


def test_all_devices_success(client):
    """All jobs reach completed status when every device succeeds."""
    payload = {
        "vlan_id": 102,
        "name": "ALLSUCCESS",
        "devices": ["mock_device", "mock_device"],
    }
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True

    time.sleep(1)

    for entry in data["jobs"]:
        resp = client.get(f"/api/v1/jobs/{entry['job_id']}")
        assert resp.json()["data"]["status"] == "completed"


def test_multi_device_unknown_device(client):
    """Unknown device in the list returns 404 before any jobs are created."""
    payload = {
        "vlan_id": 103,
        "name": "BADDEV",
        "devices": ["mock_device", "no_such_device"],
    }
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 404
    assert "no_such_device" in response.text


def test_empty_devices_rejected(client):
    """Empty devices list is rejected at schema level."""
    payload = {"vlan_id": 105, "name": "NODEV", "devices": []}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 422


def test_missing_devices_field_rejected(client):
    """Omitting devices entirely is rejected at schema level."""
    payload = {"vlan_id": 106, "name": "NOFIELD"}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 422
