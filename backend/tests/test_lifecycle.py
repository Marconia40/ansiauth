"""Tests for job/audit lifecycle consistency (no stuck pending states)."""
import time

import pytest

from app.db.models import AuditLogModel
from app.db.session import get_session
from app.models.job import Job


@pytest.fixture(scope="session", autouse=True)
def _seed_mock_devices():
    """Register "mock_device" once for this file's session.

    device_service.py's seed_defaults()/"mock_device" special-casing was
    deleted by the migration to FINAL_ARCHITECTURE.md -- api/vlans.py's
    create_vlan() needs a real, persisted Device row (require_device() 404s
    otherwise). Mirrors seed_defaults()'s old behavior: the device lives in
    a REGULAR "Mock Site" (not Base-Infrastructure, hidden from
    non-system-admins per D14) so operator_client (used throughout this
    file) has a role grant on it.

    Session-scoped so it runs before conftest's per-test
    ``_seed_test_role_users_with_full_visibility`` fixture computes which
    REGULAR sites to grant observer/operator roles on.
    """
    from app.composition import device_repository, inventory, site_repository
    from app.core.exceptions import ValidationError

    existing = site_repository.list(name="Mock Site")
    site = existing[0] if existing else site_repository.crear_con_grupo_default("Mock Site", kind="REGULAR")
    if device_repository.get("mock_device") is None:
        try:
            inventory.register(
                name="mock_device", host="192.168.1.1", vendor="cisco_ios", platform="ios",
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
    (see app/composition.py:build_plugin_registry)."""
    from app.composition import plugin_registry
    from app.services.vendors.mock import MockVendor

    mock = MockVendor()
    for vendor in ("cisco_ios", "huawei_vrp"):
        monkeypatch.setitem(plugin_registry._vendors, vendor, mock)


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    """Orquestador._ejecutar_con_retry() sleeps a real exponential backoff
    (app/services/orquestador.py, module-level `time.sleep(delay)`) between
    attempts -- there is no more per-endpoint `_RETRY_BASE_DELAY` constant
    to shrink (retry policy moved fully into Orquestador, no override hook
    exposed). No-op time.sleep in that module so the failure/retry tests
    below don't actually wait out a multi-second backoff."""
    import app.services.orquestador as orquestador_module
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda *_a, **_kw: None)


@pytest.fixture(autouse=True)
def clear_log():
    def _clear():
        with get_session() as session:
            session.query(AuditLogModel).delete(synchronize_session=False)

    _clear()
    yield
    _clear()


def _patch_create_vlan(monkeypatch, result: dict) -> None:
    """Force every MockVendor.create_vlan() call (regardless of which
    instance plugin_registry holds) to return *result* -- the modern
    equivalent of monkeypatching the deleted vlan_service.create_vlan_on_device."""
    from app.services.vendors.mock import MockVendor
    monkeypatch.setattr(MockVendor, "create_vlan", lambda self, vlan_id, name, device, password: result)


# ── Test 1: Successful execution ──────────────────────────────────────────────

def test_successful_create_ends_completed(operator_client, admin_client):
    """Job and audit must both reach a terminal 'success' state."""
    from app.composition import job_repository

    resp = operator_client.post("/api/v1/vlans/", json={"vlan_id": 601, "name": "OK", "devices": ["mock_device"]})
    job_id = resp.json()["data"]["jobs"][0]["job_id"]

    time.sleep(0.5)

    log = admin_client.get("/api/v1/audit/").json()["data"]["items"]
    # "create_vlan"/status="completed" were the legacy action/status
    # values. VLAN.aplicar() tags a successful write "crear_vlan"
    # (app/models/vlan.py's `accion` key, read by AuditRecord.desde()), and
    # AuditRecord's own status vocabulary is "success"/"failure" (job
    # status uses "completed"/"failed" instead -- 2 different fields with
    # 2 different vocabularies now). "duration_seconds" doesn't exist in
    # the current audit payload (rc/stdout/stderr/accion) either.
    entry = next(e for e in log if e["action"] == "crear_vlan")
    assert entry["status"] == "success"

    job = job_repository.get(job_id)
    assert job is not None
    assert job.status == "completed"
    assert job.finished_at is not None


# ── Test 2: Duplicate VLAN ────────────────────────────────────────────────────

def test_duplicate_vlan_ends_failed_no_pending(operator_client, admin_client, monkeypatch):
    """When Ansible reports a duplicate VLAN the job must end in failed, never stuck in pending."""
    from app.composition import job_repository

    _patch_create_vlan(monkeypatch, {"rc": 1, "stdout": "", "stderr": "VLAN already exists"})

    resp = operator_client.post("/api/v1/vlans/", json={"vlan_id": 650, "name": "DUP", "devices": ["mock_device"]})
    job_id = resp.json()["data"]["jobs"][0]["job_id"]

    time.sleep(0.5)

    # A failed resource-apply is audited as generic "recurso_fallido" (the
    # DomainEvent Orquestador dispatches on failure has no per-operation
    # "accion" in its payload -- only the success path does), not
    # "create_vlan"/"crear_vlan".
    log = admin_client.get("/api/v1/audit/").json()["data"]["items"]
    entry = next(
        (e for e in log if e["action"] == "recurso_fallido" and e["device"] == "mock_device"),
        None,
    )
    assert entry is not None
    assert entry["status"] == "failure"

    job = job_repository.get(job_id)
    assert job is not None
    assert job.status == "failed"
    assert job.status != "pending"


# ── Test 3: SSH / transient failure ──────────────────────────────────────────

def test_ssh_failure_updates_retry_count(operator_client, admin_client, monkeypatch):
    """SSH failure triggers retries and the final failed job shows retry_count > 0."""
    from app.composition import job_repository

    _patch_create_vlan(monkeypatch, {"rc": 1, "stdout": "", "stderr": "SSH connection timeout"})

    resp = operator_client.post("/api/v1/vlans/", json={"vlan_id": 602, "name": "SSH", "devices": ["mock_device"]})
    job_id = resp.json()["data"]["jobs"][0]["job_id"]

    time.sleep(0.5)

    log = admin_client.get("/api/v1/audit/").json()["data"]["items"]
    entry = next(e for e in log if e["action"] == "recurso_fallido" and e["device"] == "mock_device")
    assert entry["status"] == "failure"

    job = job_repository.get(job_id)
    assert job.status == "failed"
    assert job.retry_count > 0


# ── Test 4: Unexpected crash / ensure_final_state ─────────────────────────────

def test_ensure_final_state_clears_stuck_pending():
    """asegurar_estado_final() must force any non-terminal job to failed."""
    from app.composition import job_repository

    job = job_repository.add(Job(playbook="test.yml", device="phantom"))
    assert job.status == "pending"

    job.asegurar_estado_final()
    job_repository.add(job)

    updated = job_repository.get(job.job_id)
    assert updated.status == "failed"
    assert updated.error == "Unexpected termination"
    assert updated.finished_at is not None


def test_ensure_final_state_does_not_touch_completed():
    """asegurar_estado_final() must not overwrite an already completed job."""
    from app.composition import job_repository

    job = Job()
    job.marcar_iniciado()
    job.marcar_completado({"output": "ok"})
    job_repository.add(job)

    job.asegurar_estado_final()
    job_repository.add(job)

    updated = job_repository.get(job.job_id)
    assert updated.status == "completed"


def test_ensure_final_state_clears_stuck_running():
    """asegurar_estado_final() must also clear 'running' if somehow stuck."""
    from app.composition import job_repository

    job = Job()
    job.marcar_iniciado()
    job_repository.add(job)

    job.asegurar_estado_final()
    job_repository.add(job)

    updated = job_repository.get(job.job_id)
    assert updated.status == "failed"


# test_ensure_audit_final_state_clears_stuck_pending was deleted:
# audit_service.py (log_action/ensure_audit_final_state) no longer exists --
# AuditRepository is append-only with no "pending" status lifecycle of its
# own (per the task brief: no direct audit-side equivalent, don't invent one).


# ── Test 5: Structured error in audit ────────────────────────────────────────

def test_failed_job_has_structured_error_in_audit(operator_client, admin_client, monkeypatch):
    """Audit details must contain classified error info on ansible failure."""
    _patch_create_vlan(monkeypatch, {"rc": 1, "stdout": "", "stderr": "device unreachable"})

    operator_client.post("/api/v1/vlans/", json={"vlan_id": 603, "name": "ERR", "devices": ["mock_device"]})

    time.sleep(0.5)

    log = admin_client.get("/api/v1/audit/").json()["data"]["items"]
    entry = next(e for e in log if e["action"] == "recurso_fallido" and e["device"] == "mock_device")
    # The old nested {"error": {"type": "ansible_error", "rc":.., "stderr":..}}
    # shape is gone -- Orquestador's failure DomainEvent payload
    # (app/services/orquestador.py) carries a flat "error" string plus
    # sibling error_type/error_reason/error_summary classification fields
    # (populated by Orquestador._error_amigable()), which is this
    # architecture's actual structured-error equivalent.
    details = entry["details"]
    assert "unreachable" in details["error"]
    assert details.get("error_type") in ("permanent", "transient", "unknown")
    assert details.get("error_summary")


# ── Test 6: Jobs endpoint consistency ────────────────────────────────────────

def test_jobs_endpoint_returns_all_created_jobs(operator_client, client):
    """Every job created via the VLAN API must appear in /jobs/."""
    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 604, "name": "JOBCHECK", "devices": ["mock_device", "mock_device"]}
    )
    assert response.status_code == 202
    created_ids = {e["job_id"] for e in response.json()["data"]["jobs"]}

    all_jobs = {j["job_id"] for j in client.get("/api/v1/jobs/").json()["data"]["items"]}
    assert created_ids.issubset(all_jobs)


def test_job_fetchable_by_id_immediately_after_creation(operator_client, client):
    """Job must be retrievable by ID before execution completes."""
    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 605, "name": "IMMEDIATE", "devices": ["mock_device"]}
    )
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    resp = client.get(f"/api/v1/jobs/{job_id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["job_id"] == job_id
