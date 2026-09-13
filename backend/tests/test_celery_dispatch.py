"""Integration test: Celery task dispatch reaches the job record.

CELERY_TASK_ALWAYS_EAGER=True (set by conftest) makes .delay() run
synchronously, so job status is terminal by the time the API response
returns. Modernized: ``job_service`` is gone -- assert via
``app.composition.job_repository.get(job_id)`` instead.
"""
from fastapi.testclient import TestClient

from app.composition import (
    device_repository,
    device_sync_service,
    inventory,
    job_repository,
    plugin_registry,
    site_repository,
)
from app.core.exceptions import ValidationError as _ValidationError
from app.core.security import create_access_token
from app.main import app
from app.services.vendors.mock import MockVendor

plugin_registry.registrar("cisco_ios", MockVendor())
plugin_registry.registrar("huawei_vrp", MockVendor())

_SITE_NAME = "VLAN Orchestration Test Site"


def _ensure_site(name: str = _SITE_NAME):
    try:
        return site_repository.crear_con_grupo_default(name, kind="REGULAR")
    except ValueError:
        return site_repository.list(name=name)[0]


def _ensure_device(name: str, vendor: str = "cisco_ios", host: "str | None" = None):
    existing = device_repository.get(name)
    if existing is not None:
        return existing
    site = _ensure_site()
    try:
        device = inventory.register(
            name=name, host=host or f"10.90.0.{abs(hash(name)) % 250 + 1}",
            vendor=vendor, platform="ios",
            username="admin", password="admin123",
            site_id=site.id, device_group_id=None,
            actor={"username": "test-setup"},
        )
    except _ValidationError:
        return device_repository.get(name)
    try:
        device_sync_service.sync_vlans(device)
    except Exception:
        pass
    return device


_ensure_device("mock_device")


def _auth_client(role: str) -> TestClient:
    is_system_admin = role in {"admin", "super-admin"}
    token = create_access_token({"sub": role, "is_system_admin": is_system_admin})
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


def test_celery_always_eager_is_active():
    """Confirm the test environment runs tasks eagerly."""
    from app.worker import celery_app
    assert celery_app.conf.task_always_eager, (
        "CELERY_TASK_ALWAYS_EAGER must be True in the test environment"
    )


def test_dispatched_task_updates_job_record():
    """POST /vlans/ dispatches a Celery task that writes a terminal job status."""
    resp = _auth_client("operator").post(
        "/api/v1/vlans/",
        json={"vlan_id": 998, "name": "CeleryDispatchTest", "devices": ["mock_device"]},
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["data"]["jobs"][0]["job_id"]

    job = job_repository.get(job_id)
    assert job is not None
    assert job.status in ("completed", "failed"), (
        f"Expected terminal job status after eager dispatch, got {job.status!r}"
    )
