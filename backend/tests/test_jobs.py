import time

from app.db.models import JobModel
from app.db.session import get_session
from app.services import job_service


def test_job_lifecycle(client):
    payload = {"vlan_id": 800, "name": "TESTJOB", "devices": ["mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["data"]["status"] in ["pending", "running", "completed"]

    time.sleep(1)

    response = client.get(f"/api/v1/jobs/{job_id}")
    data = response.json()
    assert data["data"]["status"] == "completed"
    assert data["data"]["finished_at"] is not None


def test_job_failed_execution(client):
    payload = {"vlan_id": 10, "name": "FAILTEST", "devices": ["fail_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(1)

    response = client.get(f"/api/v1/jobs/{job_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["data"]["status"] == "failed"
    assert data["data"]["error"] is not None


def test_job_not_found(client):
    response = client.get("/api/v1/jobs/nonexistent-job-id")
    assert response.status_code == 404


def test_list_jobs(client):
    response = client.get("/api/v1/jobs/")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert isinstance(data["data"], list)


def test_cancel_job_success(client):
    job = job_service.create_job()
    response = client.post(f"/api/v1/jobs/{job.job_id}/cancel")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["status"] == "cancelled"


def test_get_job_requires_auth(client, observer_client, unauth_client, monkeypatch):
    job = job_service.create_job()
    monkeypatch.setattr(job_service, "get_job", lambda job_id: job)

    # No token → 401
    response = unauth_client.get(f"/api/v1/jobs/{job.job_id}")
    assert response.status_code == 401

    # Invalid/expired token → 401 (card spec says 403, but the dependency raises 401
    # for JWTError — 403 is only raised by require_role, which this endpoint does not use)
    response = unauth_client.get(
        f"/api/v1/jobs/{job.job_id}",
        headers={"Authorization": "Bearer this.is.an.expired.or.invalid.token"},
    )
    assert response.status_code == 401

    # Valid observer token → 200 (minimum role that may access this endpoint)
    response = observer_client.get(f"/api/v1/jobs/{job.job_id}")
    assert response.status_code == 200

    # Valid admin token → 200
    response = client.get(f"/api/v1/jobs/{job.job_id}")
    assert response.status_code == 200


def test_cancel_job_invalid_state(client):
    payload = {"vlan_id": 60, "name": "DONETEST", "devices": ["mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(1)

    response = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert response.status_code == 409


# ── JOB-001: DB persistence ───────────────────────────────────────────────────

def test_job_persisted_to_db(client):
    """A job created via the VLAN API must exist in the jobs table, not just in memory."""
    response = client.post("/api/v1/vlans/", json={"vlan_id": 700, "name": "PERSIST_JOB", "devices": ["mock_device"]})
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    with get_session() as session:
        row = session.query(JobModel).filter_by(job_id=job_id).first()
        assert row is not None
        assert row.job_id == job_id
        assert row.device == "mock_device"


def test_job_status_updates_reflected_in_db(client):
    """Status updates written by job runners must be visible in the DB row."""
    response = client.post("/api/v1/vlans/", json={"vlan_id": 701, "name": "STATUS_CHECK", "devices": ["mock_device"]})
    job_id = response.json()["jobs"][0]["job_id"]

    # BackgroundTasks run synchronously in TestClient — job is already complete
    with get_session() as session:
        row = session.query(JobModel).filter_by(job_id=job_id).first()
        assert row.status == "completed"
        assert row.finished_at is not None


def test_job_readable_after_service_reimport(client):
    """Re-importing job_service does not lose job records (proves no in-memory dependency)."""
    import importlib
    from app.services import job_service as svc

    response = client.post("/api/v1/vlans/", json={"vlan_id": 702, "name": "REIMPORT", "devices": ["mock_device"]})
    job_id = response.json()["jobs"][0]["job_id"]

    importlib.reload(svc)

    job = svc.get_job(job_id)
    assert job is not None
    assert job.job_id == job_id


def test_mark_orphaned_jobs_failed():
    """mark_orphaned_jobs_failed must flip any 'running' job to 'failed'."""
    job = job_service.create_job(playbook="test.yml", device="phantom")
    job_service.update_job(job.job_id, "running")

    count = job_service.mark_orphaned_jobs_failed()
    assert count >= 1

    recovered = job_service.get_job(job.job_id)
    assert recovered.status == "failed"
    assert recovered.error is not None
    assert recovered.finished_at is not None


def test_mark_orphaned_jobs_skips_terminal_states():
    """mark_orphaned_jobs_failed must leave completed and cancelled jobs untouched."""
    done = job_service.create_job()
    job_service.update_job(done.job_id, "completed", result={"output": "ok"})

    cancelled = job_service.create_job()
    job_service.cancel_job(cancelled.job_id)

    job_service.mark_orphaned_jobs_failed()

    assert job_service.get_job(done.job_id).status == "completed"
    assert job_service.get_job(cancelled.job_id).status == "cancelled"
