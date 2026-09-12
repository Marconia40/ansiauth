"""Tests for smart retry logic based on error classification.

Modernized: ``ansible_service``(alive but unused by MockVendor in mock
EXECUTION_MODE)/``audit_service``/``job_service``/``vlan_service`` are gone.
Device behavior is controlled via ``MockVendor`` (swapped into
``plugin_registry`` -- see module docstring pattern shared with the other
modernized VLAN test files); retry base delay has no
``app.api.vlans._RETRY_BASE_DELAY`` attribute anymore, so tests patch
``app.services.orquestador.time.sleep`` instead.
"""
import app.services.orquestador as orquestador_module
from app.composition import (
    audit_repository,
    device_repository,
    device_sync_service,
    inventory,
    plugin_registry,
    site_repository,
)
from app.core.exceptions import ValidationError as _ValidationError
from app.models.visibility_scope import VisibilityScope
from app.services.vendors.mock import MockVendor

plugin_registry.registrar("cisco_ios", MockVendor())
plugin_registry.registrar("huawei_vrp", MockVendor())

_SITE_NAME = "VLAN Orchestration Test Site"
_ADMIN_SCOPE = VisibilityScope(es_system_admin=True, grants=())


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


def _latest_audit_entry(device: str = "mock_device", vlan_id: "int | None" = None):
    """AuditRecord.job_id is never populated for VLAN operations dispatched
    via Orquestador's generic DomainEvent -> AuditListener path -- correlate
    on device + the human-readable summary instead (see
    test_vlan_semantics.py for the same helper/rationale)."""
    records, _total = audit_repository.query(
        scope=_ADMIN_SCOPE, device_id=device, page=1, page_size=20,
    )
    if vlan_id is not None:
        records = [r for r in records if f"VLAN {vlan_id}" in (r.summary or "")]
    return records[0]


# ── Test 1: SSH/transient error → retry exhausted ─────────────────────────────

def test_ssh_failure_retries_and_fails(operator_client, client, monkeypatch):
    """Transient SSH errors must cause 3 retries; final status must be failed."""
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda s: None)

    call_count = {"n": 0}
    delete_count = {"n": 0}

    def _ssh_failure(self, vlan_id, name, device, password):
        call_count["n"] += 1
        return {"rc": 1, "stdout": "", "stderr": "SSH connection refused to host"}

    def _delete_also_fails(self, vlan_id, device, password):
        delete_count["n"] += 1
        return {"rc": 1, "stdout": "", "stderr": "SSH connection refused to host"}

    monkeypatch.setattr(MockVendor, "create_vlan", _ssh_failure)
    monkeypatch.setattr(MockVendor, "delete_vlan", _delete_also_fails)

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 100, "name": "SSH_TEST", "devices": ["mock_device"]}
    )
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    # job.retry_count aggregates BOTH the apply-phase retries (3, transient
    # "connection refused") AND the rollback-phase retry (Orquestador wraps
    # the rollback attempt in its own _ejecutar_con_retry(max_retries=1);
    # its internal "rollback verification failed" wrapper text doesn't match
    # any known pattern, so it gets the "unknown" catch-all's 1 bonus retry
    # too) -- both phases call the same job.registrar_reintento(), so this
    # is 3 + 1 = 4, not just the create step's 3. Genuine counting behavior
    # of the current architecture, not a test artifact.
    assert job["retry_count"] == 4
    # 4 create attempts (1 initial + 3 retries)
    assert call_count["n"] == 4
    # Rollback was attempted but the delete also failed (SSH still down)
    assert job["rollback_performed"] is True
    assert job["rollback_success"] is False

    entry = _latest_audit_entry(vlan_id=100)
    assert entry.status == "failure"
    assert entry.details.get("error_type") == "transient"


# ── Test 2: Duplicate VLAN request now renames instead of failing ────────────
#
# The old "create_vlan must fail immediately when the VLAN already exists
# with a different name" scenario has no modern equivalent --
# VLAN.aplicar() unifies create/rename into one idempotent dispatch (see
# test_vlan_semantics.py's module docstring for the full explanation).
# Replaced with the surviving analogous "permanent error → 0 retries"
# scenario using a real device rejection instead.

def test_permanent_device_rejection_fails_immediately_no_retries(operator_client, client, monkeypatch):
    """A permanent-classified device rejection must fail immediately with
    retry_count=0 (no modern equivalent for the old duplicate-VLAN
    validation error -- see module note above)."""
    monkeypatch.setattr(
        MockVendor, "create_vlan",
        lambda self, vlan_id, name, device, password: {"rc": 1, "stdout": "", "stderr": "VLAN already exists"},
    )

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 501, "name": "DUPLICATE", "devices": ["mock_device"]}
    )
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["retry_count"] == 0
    assert "already exists" in job["error"]

    entry = _latest_audit_entry(vlan_id=501)
    assert entry.status == "failure"
    assert entry.details.get("error_type") == "permanent"


# ── Test 3: Normal execution → success, no retries ────────────────────────────

def test_normal_execution_succeeds_no_retries(operator_client, client):
    """A successful run must complete with retry_count=0 and status=completed."""
    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 200, "name": "OK_VLAN", "devices": ["mock_device"]}
    )
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    assert job["retry_count"] == 0

    entry = _latest_audit_entry(vlan_id=200)
    assert entry.status == "success"


# ── Test 4: Non-transient device error → no retries, permanent error_type ────

def test_permanent_ansible_error_no_retries(operator_client, client, monkeypatch):
    """A non-transient device error must fail immediately with error_type=permanent."""
    call_count = {"n": 0}
    delete_count = {"n": 0}

    def _permanent_failure(self, vlan_id, name, device, password):
        call_count["n"] += 1
        return {"rc": 1, "stdout": "", "stderr": "VLAN configuration syntax error"}

    def _delete_also_fails(self, vlan_id, device, password):
        delete_count["n"] += 1
        return {"rc": 1, "stdout": "", "stderr": "VLAN configuration syntax error"}

    monkeypatch.setattr(MockVendor, "create_vlan", _permanent_failure)
    monkeypatch.setattr(MockVendor, "delete_vlan", _delete_also_fails)

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 300, "name": "PERM_FAIL", "devices": ["mock_device"]}
    )
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    # The create step itself has 0 retries (permanent error, no retry) --
    # but the rollback attempt's own retry wrapper still contributes 1 (see
    # the detailed note in test_ssh_failure_retries_and_fails above: the
    # rollback's "rollback verification failed" wrapper text is
    # unclassified, so it gets 1 bonus retry from Orquestador's "unknown"
    # catch-all). job.retry_count is shared across apply+rollback.
    assert job["retry_count"] == 1
    # 1 create attempt (no retries — permanent error)
    assert call_count["n"] == 1
    # Rollback was attempted but the delete also failed (same device rejection)
    assert job["rollback_performed"] is True
    assert job["rollback_success"] is False

    entry = _latest_audit_entry(vlan_id=300)
    assert entry.status == "failure"
    assert entry.details.get("error_type") == "permanent"


# ── Test 5: Retry count visible via /jobs/{id} ────────────────────────────────

def test_retry_count_visible_during_execution(operator_client, client, monkeypatch):
    """retry_count and last_error must reflect all retry attempts in the final job state."""
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda s: None)

    call_number = {"n": 0}

    def _transient_then_succeed(self, vlan_id, name, device, password):
        call_number["n"] += 1
        n = call_number["n"]
        # Fail the first 2 attempts with a transient error, then succeed
        if n <= 2:
            return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(MockVendor, "create_vlan", _transient_then_succeed)

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 400, "name": "RETRY_VIS", "devices": ["mock_device"]}
    )
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    final = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert final["status"] == "completed"
    assert final["retry_count"] == 2
    assert final["last_error"] is not None
    assert "ssh" in final["last_error"].lower()
    # 2 failing attempts + 1 successful attempt
    assert call_number["n"] == 3
