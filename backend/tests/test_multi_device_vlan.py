import time

from app.services import audit_service


def test_multi_device_creates_multiple_audit_rows(client, admin_client):
    """Multi-device create produces one audit row per device, each with its own job_id."""
    audit_service.clear_audit_log()

    payload = {"vlan_id": 200, "name": "AUDITCHECK", "devices": ["mock_device", "mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200
    jobs = {entry["job_id"] for entry in response.json()["jobs"]}

    log = admin_client.get("/api/v1/audit/").json()
    entries = [e for e in log if e["action"] == "create_vlan"]

    assert len(entries) == 2

    for entry in entries:
        assert entry["device"] == "mock_device"
        assert entry["job_id"] in jobs
        assert entry["request_id"] is not None

    # All rows share the same request_id
    assert len({e["request_id"] for e in entries}) == 1

    # Each row has a distinct job_id
    assert len({e["job_id"] for e in entries}) == 2


def test_multi_device_inventory_matching(admin_client, monkeypatch):
    """Each device's job must use an inventory whose hostname matches the device name."""
    from app.services import ansible_service, vlan_service, device_service

    # Ensure the two test devices exist
    for name, host in [("cisco1", "10.10.10.1"), ("cisco2", "10.10.10.2")]:
        try:
            device_service.create_device(name, host, "cisco_ios", "admin", "cisco123")
        except ValueError:
            pass  # already seeded

    captured: list[dict] = []

    def _capture(playbook, extravars, inventory=None, device=None):
        captured.append({"inventory": inventory, "extravars": extravars})
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(ansible_service, "run_playbook", _capture)
    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")

    payload = {"vlan_id": 110, "name": "INVTEST", "devices": ["cisco1", "cisco2"]}
    response = admin_client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200
    assert len(response.json()["jobs"]) == 2

    time.sleep(1)

    assert len(captured) == 2, f"Expected 2 playbook calls, got {len(captured)}"

    seen_hosts = set()
    for call in captured:
        inv = call["inventory"]
        assert inv is not None, "Dynamic inventory was not passed to run_playbook"
        hostname = inv.split()[0]
        assert hostname in {"cisco1", "cisco2"}, f"Unexpected hostname in inventory: {hostname}"
        assert "ansible_host=" in inv
        assert "no hosts matched" not in inv.lower()
        seen_hosts.add(hostname)

    assert seen_hosts == {"cisco1", "cisco2"}, "Each device must appear in its own inventory"

    # Cleanup
    device_service.delete_device("cisco1")
    device_service.delete_device("cisco2")


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
