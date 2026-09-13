import time

import pytest

from app.db.models import JobModel
from app.db.session import get_session
from app.models.job import Job


@pytest.fixture(scope="session", autouse=True)
def _seed_mock_devices():
    """Register "mock_device"/"fail_device" once for this file's session.

    device_service.py (and its seed_defaults()/"mock_device" free-function
    special-casing) was deleted by the migration to
    FINAL_ARCHITECTURE.md -- Inventory.register() needs a real, persisted
    Device row to dispatch a VLAN write against (require_device() 404s
    otherwise, and it runs *before* any authorization check in
    api/vlans.py:create_vlan()). Mirrors device_service.seed_defaults()'s
    exact old behavior: both devices live in a REGULAR "Mock Site" (not
    Base-Infrastructure, which non-system-admins can't see per D14).

    Session-scoped (not per-test) so it runs before conftest's per-test
    ``_seed_test_role_users_with_full_visibility`` fixture computes which
    REGULAR sites to grant observer/operator roles on.
    """
    from app.composition import device_repository, inventory, site_repository
    from app.core.exceptions import ValidationError

    existing = site_repository.list(name="Mock Site")
    site = existing[0] if existing else site_repository.crear_con_grupo_default("Mock Site", kind="REGULAR")
    for name, host in (("mock_device", "192.168.1.1"), ("fail_device", "192.168.1.2")):
        if device_repository.get(name) is None:
            try:
                inventory.register(
                    name=name, host=host, vendor="cisco_ios", platform="ios",
                    username="admin", password="admin",
                    site_id=site.id, device_group_id=site.default_group_id,
                    actor={"username": "admin"},
                )
            except ValidationError:
                pass
    yield


@pytest.fixture(autouse=True)
def _force_mock_vendor_drivers(monkeypatch):
    """This backend's real .env sets EXECUTION_MODE=real, so
    app.composition.plugin_registry (built once at process import) holds
    the REAL Cisco/Huawei drivers, not MockVendor. Swap in MockVendor for
    both vendor keys for the duration of each test -- same driver instance
    code path the app itself uses when EXECUTION_MODE really is "mock"
    (see app/composition.py:build_plugin_registry). This is what makes
    "mock_device"/"fail_device" behave like the old free-function mocks."""
    from app.composition import plugin_registry
    from app.services.vendors.mock import MockVendor

    mock = MockVendor()
    for vendor in ("cisco_ios", "huawei_vrp"):
        monkeypatch.setitem(plugin_registry._vendors, vendor, mock)


def test_job_lifecycle(client):
    payload = {"vlan_id": 800, "name": "TESTJOB", "devices": ["mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 202  # POST /vlans/ is async (202 Accepted)
    job_id = response.json()["data"]["jobs"][0]["job_id"]

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
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

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
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert isinstance(data["items"], list)
    assert "total" in data
    assert "page" in data
    assert "page_size" in data


def test_cancel_job_success(client):
    from app.composition import job_repository

    job = job_repository.add(Job())
    response = client.post(f"/api/v1/jobs/{job.job_id}/cancel")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["data"]["status"] == "cancelled"


def test_get_job_requires_auth(client, observer_client, unauth_client):
    from app.composition import job_repository

    # job_repository.add() persists for real (unlike the old in-memory
    # job_service.create_job()) -- no need to monkeypatch a getter, a
    # plain GET already finds it.
    job = job_repository.add(Job())

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
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    time.sleep(1)

    response = client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert response.status_code == 409


# ── JOB-001: DB persistence ───────────────────────────────────────────────────

def test_job_persisted_to_db(client):
    """A job created via the VLAN API must exist in the jobs table, not just in memory."""
    response = client.post("/api/v1/vlans/", json={"vlan_id": 700, "name": "PERSIST_JOB", "devices": ["mock_device"]})
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    with get_session() as session:
        row = session.query(JobModel).filter_by(job_id=job_id).first()
        assert row is not None
        assert row.job_id == job_id
        assert row.device == "mock_device"


def test_job_status_updates_reflected_in_db(client):
    """Status updates written by job runners must be visible in the DB row."""
    response = client.post("/api/v1/vlans/", json={"vlan_id": 701, "name": "STATUS_CHECK", "devices": ["mock_device"]})
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    # BackgroundTasks run synchronously in TestClient — job is already complete
    with get_session() as session:
        row = session.query(JobModel).filter_by(job_id=job_id).first()
        assert row.status == "completed"
        assert row.finished_at is not None


# test_job_readable_after_service_reimport was deleted: it proved that
# reimporting job_service didn't lose an in-memory job store. job_service.py
# (and any in-memory job dict) is gone -- JobRepository always reads/writes
# the real DB table via Repository[Job], so there is nothing module-level
# left to reimport-and-lose; the scenario has no modern equivalent.


def test_mark_orphaned_jobs_failed():
    """recuperar_huerfanos() must flip any 'running' job to 'failed'."""
    from app.composition import job_repository

    job = Job(playbook="test.yml", device="phantom")
    job.marcar_iniciado()
    job_repository.add(job)

    count = job_repository.recuperar_huerfanos()
    assert count >= 1

    recovered = job_repository.get(job.job_id)
    assert recovered.status == "failed"
    assert recovered.error is not None
    assert recovered.finished_at is not None


def test_mark_orphaned_jobs_skips_terminal_states():
    """recuperar_huerfanos() must leave completed and cancelled jobs untouched."""
    from app.composition import job_repository

    done = Job()
    done.marcar_iniciado()
    done.marcar_completado({"output": "ok"})
    job_repository.add(done)

    cancelled = Job()
    cancelled.cancelar()
    job_repository.add(cancelled)

    job_repository.recuperar_huerfanos()

    assert job_repository.get(done.job_id).status == "completed"
    assert job_repository.get(cancelled.job_id).status == "cancelled"
