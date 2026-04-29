import time

def test_job_lifecycle(client):
    payload = {
        "vlan_id": 30,
        "name": "TESTJOB",
        "device": "mock_device"
    }

    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200

    job_id = response.json()["job_id"]

    # Primer check
    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] in ["pending", "running", "completed"]

    # Esperar ejecución
    time.sleep(1)

    response = client.get(f"/api/v1/jobs/{job_id}")
    data = response.json()

    assert data["status"] in ["completed", "failed"]
