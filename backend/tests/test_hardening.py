"""Step 4.4 — Device group execution hardening & final audit.

Covers: retry isolation, rollback isolation, lock release after failure,
observability terminal log, API response shape, and backward compatibility.
"""
import logging
import threading

import pytest

from app.services import ansible_service, device_service, vlan_service
import app.api.vlans as vlans_module
import app.services.vlan_execution_service as svc


def _seed(*specs):
    for name, host in specs:
        try:
            device_service.create_device(name, host, "cisco_ios", "admin", "pass")
        except ValueError:
            pass


# ── 1. Partial success correctness ───────────────────────────────────────────


def test_partial_success_true_create(client, monkeypatch):
    """Create: one device succeeds, one fails → partial_success=True in summary."""
    _seed(("hrd_c1", "10.60.1.1"))

    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda vlan_id, name, dev: {"rc": 0 if dev != "fail_device" else 1, "stdout": "", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 600, "name": "PS_CREATE", "devices": ["hrd_c1", "fail_device"]})
    assert resp.status_code == 200
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]
    assert gj["status"] == "partial_success"
    assert gj["execution_summary"]["partial_success"] is True
    assert gj["execution_summary"]["completed"] == 1
    assert gj["execution_summary"]["failed"] == 1


def test_partial_success_false_when_all_succeed(client, monkeypatch):
    """All succeed → partial_success=False."""
    _seed(("hrd_c2", "10.60.1.2"), ("hrd_c3", "10.60.1.3"))

    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 601, "name": "PS_ALL_OK", "devices": ["hrd_c2", "hrd_c3"]})
    assert resp.status_code == 200
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]
    assert gj["status"] == "completed"
    assert gj["execution_summary"]["partial_success"] is False


def test_partial_success_false_when_all_fail(client, monkeypatch):
    """All fail → partial_success=False (not mixed)."""
    _seed(("hrd_c4", "10.60.1.4"), ("hrd_c5", "10.60.1.5"))

    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 1, "stdout": "err", "stderr": "forced"})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 602, "name": "PS_ALL_FAIL", "devices": ["hrd_c4", "hrd_c5"]})
    assert resp.status_code == 200
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]
    assert gj["status"] == "failed"
    assert gj["execution_summary"]["partial_success"] is False


def test_partial_success_update_operation(client):
    """Update: fail_device mixed with a real device → partial_success."""
    _seed(("hrd_u1", "10.60.2.1"))

    resp = client.patch("/api/v1/vlans/10", json={"description": "Hardened", "devices": ["hrd_u1", "fail_device"]})
    assert resp.status_code == 200
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]
    assert gj["status"] == "partial_success"
    assert gj["execution_summary"]["partial_success"] is True


# ── 2. Retry isolation ────────────────────────────────────────────────────────


def test_retry_on_one_device_does_not_corrupt_sibling_job_state(client, monkeypatch):
    """Device 2 retries twice before succeeding. Devices 1 and 3 must have retry_count=0."""
    _seed(("ret_a", "10.60.3.1"), ("ret_b", "10.60.3.2"), ("ret_c", "10.60.3.3"))

    call_counts: dict[str, int] = {}

    def selective(vlan_id, name, device_id):
        call_counts[device_id] = call_counts.get(device_id, 0) + 1
        if device_id == "ret_b" and call_counts["ret_b"] <= 2:
            return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", selective)
    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")
    monkeypatch.setattr(vlans_module, "_RETRY_BASE_DELAY", 0.01)

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 610, "name": "RETISO", "devices": ["ret_a", "ret_b", "ret_c"]})
    assert resp.status_code == 200
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]

    results = {r["device"]: r for r in gj["device_results"]}

    # All devices completed
    assert results["ret_a"]["status"] == "completed"
    assert results["ret_b"]["status"] == "completed"
    assert results["ret_c"]["status"] == "completed"

    # Only ret_b retried; sibling devices untouched
    assert results["ret_a"]["retry_count"] == 0
    assert results["ret_b"]["retry_count"] == 2
    assert results["ret_c"]["retry_count"] == 0


def test_retry_on_one_device_does_not_block_sibling_state_after_exhaustion(client, monkeypatch):
    """Device 1 exhausts retries and fails. Device 2 must still run and complete."""
    _seed(("ret_d", "10.60.3.4"), ("ret_e", "10.60.3.5"))

    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")
    monkeypatch.setattr(vlans_module, "_RETRY_BASE_DELAY", 0.01)

    def selective(vlan_id, name, device_id):
        if device_id == "ret_d":
            return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", selective)

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 611, "name": "RETEXHAUST", "devices": ["ret_d", "ret_e"]})
    assert resp.status_code == 200
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]

    results = {r["device"]: r for r in gj["device_results"]}

    assert results["ret_d"]["status"] == "failed"
    assert results["ret_e"]["status"] == "completed"
    assert results["ret_e"]["retry_count"] == 0
    assert gj["status"] == "partial_success"


# ── 3. Rollback isolation ─────────────────────────────────────────────────────


def test_rollback_on_one_device_does_not_trigger_rollback_on_sibling(client, monkeypatch):
    """Device 1 fails and rolls back. Device 2 succeeds. Device 2 must have rollback_performed=False."""
    _seed(("rb_a", "10.60.4.1"), ("rb_b", "10.60.4.2"))

    def selective_create(vlan_id, name, device_id):
        if device_id == "rb_a":
            return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", selective_create)
    monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 0, "stdout": "deleted", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 620, "name": "RBISO", "devices": ["rb_a", "rb_b"]})
    assert resp.status_code == 200
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]

    results = {r["device"]: r for r in gj["device_results"]}

    assert results["rb_a"]["status"] == "failed"
    assert results["rb_a"]["rollback_performed"] is True

    assert results["rb_b"]["status"] == "completed"
    assert results["rb_b"]["rollback_performed"] is False
    assert results["rb_b"]["rollback_success"] is None


def test_rollback_failure_on_one_device_does_not_affect_another(client, monkeypatch):
    """Device 1 fails with a broken rollback. Device 2 still runs and succeeds."""
    _seed(("rb_c", "10.60.4.3"), ("rb_d", "10.60.4.4"))

    def selective_create(vlan_id, name, device_id):
        if device_id == "rb_c":
            return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", selective_create)
    monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 1, "stdout": "rb fail", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 621, "name": "RBFAILISO", "devices": ["rb_c", "rb_d"]})
    assert resp.status_code == 200
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]

    results = {r["device"]: r for r in gj["device_results"]}

    assert results["rb_c"]["rollback_performed"] is True
    assert results["rb_c"]["rollback_success"] is False

    assert results["rb_d"]["status"] == "completed"
    assert results["rb_d"]["rollback_performed"] is False


# ── 4. Lock correctness ───────────────────────────────────────────────────────


def test_lock_released_after_successful_job(client, monkeypatch):
    """Device lock must be released after a completed job — next job on same device runs."""
    _seed(("lck_a", "10.60.5.1"))

    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})

    r1 = client.post("/api/v1/vlans/", json={"vlan_id": 630, "name": "LCK1", "devices": ["lck_a"]})
    assert r1.status_code == 200

    from app.services import device_locks
    assert not device_locks.is_device_busy("lck_a"), "Lock must be free after job completion"


def test_lock_released_after_failed_job(client, monkeypatch):
    """Device lock must be released even when the job fails — next job on same device is unblocked."""
    _seed(("lck_b", "10.60.5.2"))

    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 1, "stdout": "", "stderr": "configuration syntax error"})

    r1 = client.post("/api/v1/vlans/", json={"vlan_id": 631, "name": "LCK2", "devices": ["lck_b"]})
    assert r1.status_code == 200

    from app.services import device_locks
    assert not device_locks.is_device_busy("lck_b"), "Lock must be free after job failure"

    # Confirm next operation on same device is not blocked
    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})
    r2 = client.post("/api/v1/vlans/", json={"vlan_id": 632, "name": "LCK3", "devices": ["lck_b"]})
    assert r2.status_code == 200
    gj2 = client.get(f"/api/v1/group-jobs/{r2.json()['group_job_id']}").json()["data"]
    assert gj2["status"] == "completed"


def test_lock_released_after_rollback(client, monkeypatch):
    """Device lock must be released after rollback so subsequent jobs are not blocked."""
    _seed(("lck_c", "10.60.5.3"))

    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 1, "stdout": "", "stderr": "configuration syntax error"})
    monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})

    r1 = client.post("/api/v1/vlans/", json={"vlan_id": 633, "name": "LCK4", "devices": ["lck_c"]})
    assert r1.status_code == 200

    from app.services import device_locks
    assert not device_locks.is_device_busy("lck_c"), "Lock must be free after rollback"


# ── 5. Observability quality ──────────────────────────────────────────────────


def test_terminal_status_logged_on_group_job_completion(client, monkeypatch, caplog):
    """group_job_service must emit a terminal status log when all devices finish."""
    _seed(("obs_a", "10.60.6.1"), ("obs_b", "10.60.6.2"))

    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})

    with caplog.at_level(logging.INFO, logger="app.services.group_job_service"):
        resp = client.post("/api/v1/vlans/", json={"vlan_id": 640, "name": "TERMLOG", "devices": ["obs_a", "obs_b"]})

    assert resp.status_code == 200
    terminal_lines = [r.message for r in caplog.records if "terminal status" in r.message]
    assert len(terminal_lines) == 1
    assert "completed" in terminal_lines[0]


def test_group_outcome_log_emitted_after_runner(client, monkeypatch, caplog):
    """Group runner must log final outcome (status + completed count) after the loop."""
    _seed(("obs_c", "10.60.6.3"))

    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})

    with caplog.at_level(logging.INFO, logger="app.services.vlan_execution_service"):
        resp = client.post("/api/v1/vlans/", json={"vlan_id": 641, "name": "OUTLOG", "devices": ["obs_c"]})

    assert resp.status_code == 200
    outcome_lines = [r.message for r in caplog.records if "create complete" in r.message]
    assert len(outcome_lines) == 1
    assert "completed" in outcome_lines[0]


def test_partial_success_terminal_log_reflects_outcome(client, monkeypatch, caplog):
    """Terminal log must say partial_success when some devices fail."""
    _seed(("obs_d", "10.60.6.4"))

    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda vlan_id, name, dev: {"rc": 0 if dev != "fail_device" else 1, "stdout": "", "stderr": ""})

    with caplog.at_level(logging.INFO, logger="app.services.group_job_service"):
        resp = client.post("/api/v1/vlans/", json={"vlan_id": 642, "name": "PSLOG", "devices": ["obs_d", "fail_device"]})

    assert resp.status_code == 200
    terminal_lines = [r.message for r in caplog.records if "terminal status" in r.message]
    assert len(terminal_lines) == 1
    assert "partial_success" in terminal_lines[0]


# ── 6. API clarity ────────────────────────────────────────────────────────────


def test_group_job_api_response_shape(client, monkeypatch):
    """GET /group-jobs/{id} must have all required top-level and summary fields."""
    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 650, "name": "SHAPE", "devices": ["mock_device"]})
    assert resp.status_code == 200
    gj_resp = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()
    assert gj_resp["success"] is True

    gj = gj_resp["data"]
    for field in ("group_job_id", "status", "operation", "execution_summary", "device_results",
                  "created_at", "started_at", "finished_at"):
        assert field in gj, f"Missing top-level field: {field}"

    s = gj["execution_summary"]
    for field in ("total_devices", "completed", "failed", "partial_success", "rollback_count", "duration_ms"):
        assert field in s, f"Missing summary field: {field}"

    dr = gj["device_results"][0]
    for field in ("device", "job_id", "status", "retry_count", "rollback_performed",
                  "rollback_success", "duration_ms", "current_step"):
        assert field in dr, f"Missing device_result field: {field}"


def test_individual_job_api_response_shape(client, monkeypatch):
    """GET /jobs/{id} must include execution_summary, current_step, group_job_id, retry_count."""
    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 651, "name": "JOBSHAPE", "devices": ["mock_device"]})
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["job_id"]

    job_resp = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    for field in ("job_id", "status", "current_step", "retry_count", "rollback_performed",
                  "rollback_success", "group_job_id", "execution_summary"):
        assert field in job_resp, f"Missing job field: {field}"

    summary = job_resp["execution_summary"]
    for field in ("attempts", "rollback_performed", "rollback_success", "duration_ms"):
        assert field in summary, f"Missing job summary field: {field}"


# ── 7. Backward compatibility ─────────────────────────────────────────────────


def test_single_device_create_response_shape_unchanged(client, monkeypatch):
    """Single-device create response must have success, jobs list, group_job_id — nothing removed."""
    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 660, "name": "COMPAT", "devices": ["mock_device"]})
    assert resp.status_code == 200
    data = resp.json()

    assert data["success"] is True
    assert isinstance(data["jobs"], list)
    assert len(data["jobs"]) == 1
    assert "group_job_id" in data
    job0 = data["jobs"][0]
    assert "job_id" in job0
    assert "device" in job0
    assert "status" in job0


def test_single_device_job_runs_and_completes(client, monkeypatch):
    """Single-device flow must still result in a completed job."""
    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 661, "name": "COMPAT2", "devices": ["mock_device"]})
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["job_id"]
    job_resp = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job_resp["status"] == "completed"


def test_delete_single_device_backward_compat(client, monkeypatch):
    """Single-device delete must still return jobs list and group_job_id."""
    from app.models.vlan import VLANInfo

    call_n = {"n": 0}

    def counting_get(device_id=None):
        call_n["n"] += 1
        if call_n["n"] <= 2:
            return [VLANInfo(vlan_id=662, name="DCOMPAT")]
        return []

    monkeypatch.setattr(vlan_service, "get_vlans", counting_get)
    monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.request("DELETE", "/api/v1/vlans/662", json={"devices": ["mock_device"]})
    assert resp.status_code == 200
    data = resp.json()
    assert "group_job_id" in data
    assert isinstance(data["jobs"], list)
    assert len(data["jobs"]) == 1


def test_update_single_device_backward_compat(client, monkeypatch):
    """Single-device update must still return jobs list and group_job_id."""
    monkeypatch.setattr(vlan_service, "update_vlan_description",
                        lambda *a: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.patch("/api/v1/vlans/10", json={"description": "Compat", "devices": ["mock_device"]})
    assert resp.status_code == 200
    data = resp.json()
    assert "group_job_id" in data
    assert isinstance(data["jobs"], list)
