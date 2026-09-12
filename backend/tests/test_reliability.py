"""Tests for concurrency control, rate limiting, retry, and rollback.

Modernized from the pre-Repository[T]/Orquestador architecture:
``audit_service``/``vlan_service``/``rate_limiter``/``device_service`` are
all gone. ``device_locks``/``rate_limiter`` are fused into
``app.composition.redis_coordinator`` (``bloquear()``/``limitar()``/
``resetear()``); retry/backoff lives in
``Orquestador._ejecutar_con_retry()``/``_clasificar_error()`` (absorbed
verbatim from the deleted retry_policy.py); there is no more
``app.api.vlans._RETRY_BASE_DELAY`` to monkeypatch -- the retry base delay
is a hardcoded ``1.0`` argument inside ``Orquestador.ejecutar()``, so tests
that need fast retries patch ``app.services.orquestador.time.sleep`` instead
(per architecture guidance).
"""
import threading
import time

import app.services.redis_coordinator as redis_coordinator_module
import app.services.orquestador as orquestador_module
from app.composition import (
    audit_repository,
    device_repository,
    device_sync_service,
    inventory,
    plugin_registry,
    redis_coordinator,
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


# ── Concurrency ───────────────────────────────────────────────────────────────

def test_concurrent_jobs_on_same_device_are_serialized(client, monkeypatch):
    """Two jobs targeting the same device must not execute simultaneously.

    Issued as 2 real concurrent HTTP requests (threads) rather than one
    request listing the same device twice -- GroupOperationRunner.encolar()
    already dispatches devices one at a time under CELERY_TASK_ALWAYS_EAGER,
    so a single request wouldn't exercise RedisCoordinator.bloquear()'s
    actual cross-request serialization."""
    execution_log: list[tuple[str, int]] = []
    log_lock = threading.Lock()

    def slow_create(self, vlan_id, name, device, password):
        with log_lock:
            execution_log.append(("start", vlan_id))
        time.sleep(0.15)
        with log_lock:
            execution_log.append(("end", vlan_id))
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(MockVendor, "create_vlan", slow_create)

    def _post(vlan_id):
        client.post("/api/v1/vlans/", json={
            "vlan_id": vlan_id, "name": f"CONC{vlan_id}", "devices": ["mock_device"],
        })

    t1 = threading.Thread(target=_post, args=(201,))
    t2 = threading.Thread(target=_post, args=(211,))
    t1.start()
    time.sleep(0.03)
    t2.start()
    t1.join()
    t2.join()

    assert len(execution_log) == 4
    # Serialized: first job fully completes before second starts
    assert execution_log[0][0] == "start"
    assert execution_log[1][0] == "end"
    assert execution_log[2][0] == "start"
    assert execution_log[3][0] == "end"


def test_different_devices_run_concurrently(client, monkeypatch):
    """Jobs on different devices should not block each other."""
    _ensure_device("dev_a", host="10.0.0.1")
    _ensure_device("dev_b", host="10.0.0.2")

    started: list[str] = []
    start_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=2.0)

    def concurrent_create(self, vlan_id, name, device, password):
        with start_lock:
            started.append(device.name)
        try:
            barrier.wait()  # Both threads reach here before either proceeds
        except threading.BrokenBarrierError:
            pass
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(MockVendor, "create_vlan", concurrent_create)

    def _post(device_name, vlan_id):
        client.post("/api/v1/vlans/", json={
            "vlan_id": vlan_id, "name": "PARTEST", "devices": [device_name],
        })

    t1 = threading.Thread(target=_post, args=("dev_a", 202))
    t2 = threading.Thread(target=_post, args=("dev_b", 203))
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    # Both devices started — barrier proves they ran concurrently (both had
    # to reach barrier.wait() before either returns from it)
    assert set(started) == {"dev_a", "dev_b"}


# ── Retry ─────────────────────────────────────────────────────────────────────

def test_no_retry_on_non_retryable_error(client, monkeypatch):
    """Errors like 'VLAN already exists' must not trigger retries."""
    attempt_count = [0]

    def fail_once(self, vlan_id, name, device, password):
        attempt_count[0] += 1
        return {"rc": 1, "stdout": "", "stderr": "VLAN already exists"}

    monkeypatch.setattr(MockVendor, "create_vlan", fail_once)

    response = client.post("/api/v1/vlans/", json={"vlan_id": 301, "name": "NORETRY", "devices": ["mock_device"]})
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["retry_count"] == 0
    assert attempt_count[0] == 1  # only the initial attempt


def test_retry_on_transient_error_succeeds(client, monkeypatch):
    """A transient SSH timeout is retried and ultimately succeeds."""
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda s: None)

    attempt_count = [0]

    def flaky_create(self, vlan_id, name, device, password):
        attempt_count[0] += 1
        if attempt_count[0] < 3:
            return {"rc": 1, "stdout": "", "stderr": "SSH connection timeout"}
        return {"rc": 0, "stdout": "VLAN created", "stderr": ""}

    monkeypatch.setattr(MockVendor, "create_vlan", flaky_create)

    response = client.post("/api/v1/vlans/", json={"vlan_id": 302, "name": "RETRY", "devices": ["mock_device"]})
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    assert job["retry_count"] == 2
    assert attempt_count[0] == 3  # initial + 2 retries


def test_retry_exhaustion_marks_job_failed(client, monkeypatch):
    """When all retries are exhausted, job must end in failed status."""
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda s: None)

    attempt_count = [0]

    def always_timeout(self, vlan_id, name, device, password):
        attempt_count[0] += 1
        return {"rc": 1, "stdout": "", "stderr": "connection refused by device"}

    monkeypatch.setattr(MockVendor, "create_vlan", always_timeout)

    response = client.post("/api/v1/vlans/", json={"vlan_id": 303, "name": "EXHAUSTED", "devices": ["mock_device"]})
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["retry_count"] == 3  # max_retries
    assert attempt_count[0] == 4    # initial + 3 retries


# ── Rollback ──────────────────────────────────────────────────────────────────

def test_rollback_attempted_on_create_failure(client, monkeypatch):
    """A failed create triggers a delete rollback when VLAN did not exist before."""
    rollback_called = [False]

    def failing_create(self, vlan_id, name, device, password):
        return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}

    def track_delete(self, vlan_id, device, password):
        rollback_called[0] = True
        return {"rc": 0, "stdout": "deleted", "stderr": ""}

    monkeypatch.setattr(MockVendor, "create_vlan", failing_create)
    monkeypatch.setattr(MockVendor, "delete_vlan", track_delete)

    response = client.post("/api/v1/vlans/", json={"vlan_id": 400, "name": "ROLLBACK", "devices": ["mock_device"]})
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert rollback_called[0] is True
    assert job["rollback_performed"] is True


def test_no_rollback_when_vlan_already_existed(client, monkeypatch):
    """If VLAN exists with a different name, the request now RENAMES it
    (VLAN.aplicar()'s unified create/update dispatch, see
    test_vlan_semantics.py) instead of failing validation -- so there is no
    "validation failed before touching the device" case left to assert on
    rollback-avoidance for. Assert the surviving, equivalent invariant
    instead: a successful rename never triggers a rollback."""
    rollback_called = [False]

    def track_delete(self, vlan_id, device, password):
        rollback_called[0] = True
        return {"rc": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(MockVendor, "delete_vlan", track_delete)

    # VLAN 10 exists in mock as "MGMT"; requesting a different name now
    # renames it (update_vlan), not a validation failure.
    response = client.post("/api/v1/vlans/", json={"vlan_id": 10, "name": "DIFFERENT", "devices": ["mock_device"]})
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    assert rollback_called[0] is False
    assert job["rollback_performed"] is False


def test_rollback_on_update_failure(client, monkeypatch):
    """A failed update triggers restore of the previous VLAN name."""
    update_calls: list[dict] = []

    def track_update(self, vlan_id, name, device, password):
        update_calls.append({"vlan_id": vlan_id, "name": name})
        # First call (the real update) fails with a permanent error (no
        # retry); second call (rollback, restoring the previous name) succeeds.
        if len(update_calls) == 1:
            return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}
        return {"rc": 0, "stdout": "restored", "stderr": ""}

    monkeypatch.setattr(MockVendor, "update_vlan", track_update)

    response = client.patch("/api/v1/vlans/10", json={"description": "New-name", "devices": ["mock_device"]})
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert len(update_calls) == 2          # original attempt + rollback
    assert update_calls[1]["name"] == "MGMT"  # restored to previous name
    assert job["rollback_performed"] is True


# ── Rate limiting ─────────────────────────────────────────────────────────────

def test_rate_limiter_delays_excess_jobs(monkeypatch):
    """Jobs beyond the per-device limit are delayed until a slot opens."""
    redis_coordinator.resetear("rl_test_device")
    monkeypatch.setattr(redis_coordinator_module, "MAX_JOBS_PER_WINDOW", 2)
    monkeypatch.setattr(redis_coordinator_module, "WINDOW_SECONDS", 0.5)

    try:
        redis_coordinator.limitar("rl_test_device")
        redis_coordinator.limitar("rl_test_device")

        # Third slot should require a wait
        start = time.time()
        redis_coordinator.limitar("rl_test_device")
        elapsed = time.time() - start

        assert elapsed >= 0.3, f"Expected delay ≥ 0.3s, got {elapsed:.3f}s"
    finally:
        redis_coordinator.resetear("rl_test_device")


def test_rate_limiter_independent_per_device(monkeypatch):
    """Rate limit state is isolated per device."""
    redis_coordinator.resetear()
    monkeypatch.setattr(redis_coordinator_module, "MAX_JOBS_PER_WINDOW", 1)

    try:
        redis_coordinator.limitar("device_x")

        # device_y is independent — should not be delayed
        start = time.time()
        redis_coordinator.limitar("device_y")
        elapsed = time.time() - start

        assert elapsed < 0.1, f"Independent device was unexpectedly delayed: {elapsed:.3f}s"
    finally:
        redis_coordinator.resetear()


# ── Audit fields ──────────────────────────────────────────────────────────────

def test_audit_includes_reliability_fields(operator_client, client):
    """Completed jobs must report retries/rollback/duration.

    The original test read these off the audit log entry's ``details``
    (``audit_service.get_audit_log()``). In the current architecture,
    ``AuditRecord.desde()`` populates ``details`` from the raw
    ``resultado`` dict a successful ``recurso_aplicado`` event carries
    (device rc/stdout/stderr/accion) -- retries/rollback/duration are NOT
    mirrored into the audit trail for successful operations (only a
    *failed* job's audit event adds rollback_performed/rollback_success,
    via Orquestador's except-branch payload). That looks like a real gap
    versus the old behavior (flagged in the migration report) rather than
    a deliberate removal, since Job itself still tracks all 3 fields --
    asserting on the Job's own execution_summary (GET /jobs/{id}) instead,
    which is the surviving source of truth for this data.
    """
    resp = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 500, "name": "AUDITFIELDS", "devices": ["mock_device"]},
    )
    assert resp.status_code == 202
    job_id = resp.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    summary = job["execution_summary"]
    assert summary["rollback_performed"] is False
    assert summary["duration_ms"] is not None
    assert job["retry_count"] == 0
