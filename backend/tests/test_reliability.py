"""Tests for concurrency control, rate limiting, retry, and rollback."""
import time
import threading

import pytest

from app.services import audit_service, vlan_service


@pytest.fixture(autouse=True)
def clear_log():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


# ── Concurrency ───────────────────────────────────────────────────────────────

def test_concurrent_jobs_on_same_device_are_serialized(client, monkeypatch):
    """Two jobs targeting the same device must not execute simultaneously."""
    execution_log: list[tuple[str, int]] = []
    log_lock = threading.Lock()

    def slow_create(vlan_id, name, device_id):
        with log_lock:
            execution_log.append(("start", vlan_id))
        time.sleep(0.1)
        with log_lock:
            execution_log.append(("end", vlan_id))
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", slow_create)

    response = client.post("/api/v1/vlans/", json={
        "vlan_id": 201, "name": "CONCTEST", "devices": ["mock_device", "mock_device"],
    })
    assert response.status_code == 200

    time.sleep(0.6)  # let both threads finish

    assert len(execution_log) == 4
    # Serialized: first job fully completes before second starts
    # Pattern must be: start, end, start, end  (not start, start, end, end)
    assert execution_log[0][0] == "start"
    assert execution_log[1][0] == "end"
    assert execution_log[2][0] == "start"
    assert execution_log[3][0] == "end"


def test_different_devices_run_concurrently(client, monkeypatch):
    """Jobs on different devices should not block each other."""
    started: list[str] = []
    start_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=2.0)

    def concurrent_create(vlan_id, name, device_id):
        with start_lock:
            started.append(device_id)
        try:
            barrier.wait()  # Both threads reach here before either proceeds
        except threading.BrokenBarrierError:
            pass
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    from app.services import device_service
    for name, host in [("dev_a", "10.0.0.1"), ("dev_b", "10.0.0.2")]:
        try:
            device_service.create_device(name, host, "cisco_ios", "admin", "pass123")
        except ValueError:
            pass

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", concurrent_create)

    response = client.post("/api/v1/vlans/", json={
        "vlan_id": 202, "name": "PARTEST", "devices": ["dev_a", "dev_b"],
    })
    assert response.status_code == 200

    time.sleep(1.0)

    # Both devices started — barrier proves they ran concurrently
    assert set(started) == {"dev_a", "dev_b"}

    device_service.delete_device("dev_a")
    device_service.delete_device("dev_b")


# ── Retry ─────────────────────────────────────────────────────────────────────

def test_no_retry_on_non_retryable_error(client, monkeypatch):
    """Errors like 'VLAN already exists' must not trigger retries."""
    import app.api.vlans as vlans_module

    attempt_count = [0]

    def fail_once(vlan_id, name, device_id):
        attempt_count[0] += 1
        return {"rc": 1, "stdout": "", "stderr": "VLAN already exists"}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", fail_once)

    response = client.post("/api/v1/vlans/", json={"vlan_id": 301, "name": "NORETRY", "devices": ["mock_device"]})
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["retry_count"] == 0
    assert attempt_count[0] == 1  # only the initial attempt


def test_retry_on_transient_error_succeeds(client, monkeypatch):
    """A transient SSH timeout is retried and ultimately succeeds."""
    import app.api.vlans as vlans_module

    monkeypatch.setattr(vlans_module, "_RETRY_BASE_DELAY", 0.001)

    attempt_count = [0]

    def flaky_create(vlan_id, name, device_id):
        attempt_count[0] += 1
        if attempt_count[0] < 3:
            return {"rc": 1, "stdout": "", "stderr": "SSH connection timeout"}
        return {"rc": 0, "stdout": "VLAN created", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", flaky_create)

    response = client.post("/api/v1/vlans/", json={"vlan_id": 302, "name": "RETRY", "devices": ["mock_device"]})
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    assert job["retry_count"] == 2
    assert attempt_count[0] == 3  # initial + 2 retries


def test_retry_exhaustion_marks_job_failed(client, monkeypatch):
    """When all retries are exhausted, job must end in failed status."""
    import app.api.vlans as vlans_module

    monkeypatch.setattr(vlans_module, "_RETRY_BASE_DELAY", 0.001)

    attempt_count = [0]

    def always_timeout(vlan_id, name, device_id):
        attempt_count[0] += 1
        return {"rc": 1, "stdout": "", "stderr": "connection refused by device"}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", always_timeout)

    response = client.post("/api/v1/vlans/", json={"vlan_id": 303, "name": "EXHAUSTED", "devices": ["mock_device"]})
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["retry_count"] == 3  # max_retries
    assert attempt_count[0] == 4   # initial + 3 retries


# ── Rollback ──────────────────────────────────────────────────────────────────

def test_rollback_attempted_on_create_failure(client, monkeypatch):
    """A failed create triggers a delete rollback when VLAN did not exist before."""
    import app.api.vlans as vlans_module

    rollback_called = [False]

    def failing_create(vlan_id, name, device_id):
        return {"rc": 1, "stdout": "", "stderr": "Config push failed"}

    def track_delete(vlan_id, device_id):
        rollback_called[0] = True
        return {"rc": 0, "stdout": "deleted", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", failing_create)
    monkeypatch.setattr(vlan_service, "delete_vlan", track_delete)

    response = client.post("/api/v1/vlans/", json={"vlan_id": 400, "name": "ROLLBACK", "devices": ["mock_device"]})
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert rollback_called[0] is True
    assert job["rollback_performed"] is True


def test_no_rollback_when_vlan_already_existed(client, monkeypatch):
    """If VLAN exists with a different name, validation fails immediately with no rollback."""
    rollback_called = [False]

    def track_delete(vlan_id, device_id):
        rollback_called[0] = True
        return {"rc": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(vlan_service, "delete_vlan", track_delete)

    # VLAN 10 exists in mock as "MGMT"; requesting a different name triggers validation error
    response = client.post("/api/v1/vlans/", json={"vlan_id": 10, "name": "DIFFERENT", "devices": ["mock_device"]})
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    time.sleep(0.5)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert rollback_called[0] is False
    assert job["rollback_performed"] is False


def test_rollback_on_update_failure(client, monkeypatch):
    """A failed update triggers restore of the previous VLAN name."""
    update_calls: list[dict] = []

    def track_update(vlan_id, description, device_id):
        update_calls.append({"vlan_id": vlan_id, "description": description})
        # First call (the real update) fails with a permanent error (no retry);
        # second call (rollback) succeeds.
        if len(update_calls) == 1:
            return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}
        return {"rc": 0, "stdout": "restored", "stderr": ""}

    monkeypatch.setattr(vlan_service, "update_vlan_description", track_update)

    response = client.patch("/api/v1/vlans/10", json={"description": "New name", "devices": ["mock_device"]})
    assert response.status_code == 200
    job_id = response.json()["jobs"][0]["job_id"]

    # BackgroundTasks run synchronously in TestClient
    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert len(update_calls) == 2          # original attempt + rollback
    assert update_calls[1]["description"] == "MGMT"  # restored to previous name
    assert job["rollback_performed"] is True


# ── Rate limiting ─────────────────────────────────────────────────────────────

def test_rate_limiter_delays_excess_jobs():
    """Jobs beyond the per-device limit are delayed until a slot opens."""
    from app.services import rate_limiter

    rate_limiter.reset("rl_test_device")

    saved_max = rate_limiter.MAX_JOBS_PER_WINDOW
    saved_window = rate_limiter.WINDOW_SECONDS
    rate_limiter.MAX_JOBS_PER_WINDOW = 2
    rate_limiter.WINDOW_SECONDS = 0.5

    try:
        rate_limiter.wait_for_slot("rl_test_device")
        rate_limiter.wait_for_slot("rl_test_device")

        # Third slot should require a wait
        start = time.time()
        rate_limiter.wait_for_slot("rl_test_device")
        elapsed = time.time() - start

        assert elapsed >= 0.3, f"Expected delay ≥ 0.3s, got {elapsed:.3f}s"
    finally:
        rate_limiter.MAX_JOBS_PER_WINDOW = saved_max
        rate_limiter.WINDOW_SECONDS = saved_window
        rate_limiter.reset("rl_test_device")


def test_rate_limiter_independent_per_device():
    """Rate limit state is isolated per device."""
    from app.services import rate_limiter

    rate_limiter.reset()

    saved_max = rate_limiter.MAX_JOBS_PER_WINDOW
    rate_limiter.MAX_JOBS_PER_WINDOW = 1

    try:
        rate_limiter.wait_for_slot("device_x")

        # device_y is independent — should not be delayed
        start = time.time()
        rate_limiter.wait_for_slot("device_y")
        elapsed = time.time() - start

        assert elapsed < 0.1, f"Independent device was unexpectedly delayed: {elapsed:.3f}s"
    finally:
        rate_limiter.MAX_JOBS_PER_WINDOW = saved_max
        rate_limiter.reset()


# ── Audit fields ──────────────────────────────────────────────────────────────

def test_audit_includes_reliability_fields(operator_client, admin_client):
    """Completed audit entries must include retries, rollback_performed, duration_seconds."""
    operator_client.post("/api/v1/vlans/", json={"vlan_id": 500, "name": "AUDITFIELDS", "devices": ["mock_device"]})

    time.sleep(0.5)

    log = admin_client.get("/api/v1/audit/").json()
    entry = next(e for e in log if e["action"] == "create_vlan" and e["details"].get("vlan_id") == 500)
    assert entry["status"] == "completed"
    assert "retries" in entry["details"]
    assert "rollback_performed" in entry["details"]
    assert "duration_seconds" in entry["details"]
    assert entry["details"]["retries"] == 0
    assert entry["details"]["rollback_performed"] is False
