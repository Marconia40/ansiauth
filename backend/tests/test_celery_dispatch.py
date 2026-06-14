"""Integration test: Celery task dispatch reaches the job record.

CELERY_TASK_ALWAYS_EAGER=True (set by conftest) makes .delay() run
synchronously, so job status is terminal by the time the API response
returns.
"""
import os
import pytest
from fastapi.testclient import TestClient
from app.core.security import create_access_token
from app.main import app
from app.services import job_service


def _auth_client(role: str) -> TestClient:
    token = create_access_token({"sub": role, "role": role})
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def test_celery_always_eager_is_active():
    """Confirm the test environment runs tasks eagerly."""
    from app.worker import celery_app
    assert celery_app.conf.task_always_eager, (
        "CELERY_TASK_ALWAYS_EAGER must be True in the test environment"
    )


def test_dispatched_task_updates_job_record(monkeypatch):
    """POST /vlans/ dispatches a Celery task that writes a terminal job status."""
    from app.services import ansible_service
    monkeypatch.setattr(
        ansible_service, "run_playbook",
        lambda *a, **kw: {"rc": 0, "stdout": "ok", "stderr": ""},
    )

    resp = _auth_client("operator").post(
        "/api/v1/vlans/",
        json={"vlan_id": 998, "name": "CeleryDispatchTest", "devices": ["mock_device"]},
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["jobs"][0]["job_id"]

    job = job_service.get_job(job_id)
    assert job is not None
    assert job.status in ("completed", "failed"), (
        f"Expected terminal job status after eager dispatch, got {job.status!r}"
    )
