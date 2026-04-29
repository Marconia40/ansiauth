import time

from app.services import job_service


def test_job_lifecycle(client):
    payload = {
        "vlan_id": 30,
        "name": "TESTJOB",
        "device": "mock_device"
    }

    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200

    job_id = response.json()["job_id"]

    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ["pending", "running", "completed"]

    time.sleep(1)

    response = client.get(f"/api/v1/jobs/{job_id}")
    data = response.json()
    assert data["status"] == "completed"
    assert data["finished_at"] is not None


def test_job_failed_execution(client):
    payload = {
        "vlan_id": 10,
        "name": "FAILTEST",
        "device": "fail_device"
    }

    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200

    job_id = response.json()["job_id"]

    time.sleep(1)

    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "failed"
    assert "error" in data["result"]


def test_job_not_found(client):
    response = client.get("/api/v1/jobs/nonexistent-job-id")
    assert response.status_code == 404


def test_cancel_job_success(client):
    job = job_service.create_job()

    response = client.post(f"/api/v1/jobs/{job.job_id}/cancel")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "cancelled"


def test_cancel_job_invalid_state(client):
    payload = {
        "vlan_id": 60,
        "name": "DONETEST",
        "device": "mock_device"
    }

    response = client.post("/api/v1/vlans/", json=payload)
    job_id = response.json()["job_id"]

    time.sleep(1)

    response = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert response.status_code == 409
