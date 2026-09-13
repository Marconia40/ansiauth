"""Step 4.4 — Device group execution hardening & final audit.

Covers: retry isolation, rollback isolation, lock release after failure,
observability terminal log, API response shape, and backward compatibility.

Modernized: ``ansible_service``(still alive, but MockVendor doesn't call it
in mock EXECUTION_MODE)/``device_service``/``vlan_service`` are gone;
``app.services.vlan_execution_service`` is fully deleted (its retry/rollback
logic now lives on ``Orquestador``); ``device_locks`` is fused into
``app.composition.redis_coordinator`` (``esta_ocupado()``).
"""
import logging

from app.composition import (
    device_repository,
    device_sync_service,
    inventory,
    plugin_registry,
    redis_coordinator,
    site_repository,
)
from app.core.exceptions import ValidationError as _ValidationError
import app.services.orquestador as orquestador_module
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


def _seed(*specs):
    for name, host in specs:
        _ensure_device(name, host=host)


_ensure_device("mock_device")
_ensure_device("fail_device")


def _fast_retries(monkeypatch):
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda s: None)


# ── 1. Partial success correctness ───────────────────────────────────────────


def test_partial_success_true_create(client, monkeypatch):
    """Create: one device succeeds, one fails → partial_success=True in summary."""
    _seed(("hrd_c1", "10.60.1.1"))

    monkeypatch.setattr(
        MockVendor, "create_vlan",
        lambda self, vlan_id, name, device, password: (
            {"rc": 0, "stdout": "ok", "stderr": ""} if device.name != "fail_device"
            else {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}
        ),
    )

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 600, "name": "PS_CREATE", "devices": ["hrd_c1", "fail_device"]})
    assert resp.status_code == 202
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['data']['group_job_id']}").json()["data"]
    assert gj["status"] == "partial_success"
    assert gj["execution_summary"]["partial_success"] is True
    assert gj["execution_summary"]["completed"] == 1
    assert gj["execution_summary"]["failed"] == 1


def test_partial_success_false_when_all_succeed(client, monkeypatch):
    """All succeed → partial_success=False."""
    _seed(("hrd_c2", "10.60.1.2"), ("hrd_c3", "10.60.1.3"))

    monkeypatch.setattr(
        MockVendor, "create_vlan",
        lambda self, vlan_id, name, device, password: {"rc": 0, "stdout": "ok", "stderr": ""},
    )

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 601, "name": "PS_ALL_OK", "devices": ["hrd_c2", "hrd_c3"]})
    assert resp.status_code == 202
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['data']['group_job_id']}").json()["data"]
    assert gj["status"] == "completed"
    assert gj["execution_summary"]["partial_success"] is False


def test_partial_success_false_when_all_fail(client, monkeypatch):
    """All fail → partial_success=False (not mixed)."""
    _seed(("hrd_c4", "10.60.1.4"), ("hrd_c5", "10.60.1.5"))

    monkeypatch.setattr(
        MockVendor, "create_vlan",
        lambda self, vlan_id, name, device, password: {"rc": 1, "stdout": "", "stderr": "configuration syntax error"},
    )

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 602, "name": "PS_ALL_FAIL", "devices": ["hrd_c4", "hrd_c5"]})
    assert resp.status_code == 202
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['data']['group_job_id']}").json()["data"]
    assert gj["status"] == "failed"
    assert gj["execution_summary"]["partial_success"] is False


def test_partial_success_update_operation(client):
    """Update: fail_device mixed with a real device → partial_success."""
    _seed(("hrd_u1", "10.60.2.1"))

    resp = client.patch("/api/v1/vlans/10", json={"description": "Hardened", "devices": ["hrd_u1", "fail_device"]})
    assert resp.status_code == 202
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['data']['group_job_id']}").json()["data"]
    assert gj["status"] == "partial_success"
    assert gj["execution_summary"]["partial_success"] is True


# ── 2. Retry isolation ────────────────────────────────────────────────────────


def test_retry_on_one_device_does_not_corrupt_sibling_job_state(client, monkeypatch):
    """Device 2 retries twice before succeeding. Devices 1 and 3 must have retry_count=0."""
    _seed(("ret_a", "10.60.3.1"), ("ret_b", "10.60.3.2"), ("ret_c", "10.60.3.3"))
    _fast_retries(monkeypatch)

    call_counts: dict[str, int] = {}

    def selective(self, vlan_id, name, device, password):
        call_counts[device.name] = call_counts.get(device.name, 0) + 1
        if device.name == "ret_b" and call_counts["ret_b"] <= 2:
            return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(MockVendor, "create_vlan", selective)

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 610, "name": "RETISO", "devices": ["ret_a", "ret_b", "ret_c"]})
    assert resp.status_code == 202
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['data']['group_job_id']}").json()["data"]

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
    _fast_retries(monkeypatch)

    def selective(self, vlan_id, name, device, password):
        if device.name == "ret_d":
            return {"rc": 1, "stdout": "SSH connection refused", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(MockVendor, "create_vlan", selective)

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 611, "name": "RETEXHAUST", "devices": ["ret_d", "ret_e"]})
    assert resp.status_code == 202
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['data']['group_job_id']}").json()["data"]

    results = {r["device"]: r for r in gj["device_results"]}

    assert results["ret_d"]["status"] == "failed"
    assert results["ret_e"]["status"] == "completed"
    assert results["ret_e"]["retry_count"] == 0
    assert gj["status"] == "partial_success"


# ── 3. Rollback isolation ─────────────────────────────────────────────────────


def test_rollback_on_one_device_does_not_trigger_rollback_on_sibling(client, monkeypatch):
    """Device 1 fails and rolls back. Device 2 succeeds. Device 2 must have rollback_performed=False."""
    _seed(("rb_a", "10.60.4.1"), ("rb_b", "10.60.4.2"))

    def selective_create(self, vlan_id, name, device, password):
        if device.name == "rb_a":
            return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(MockVendor, "create_vlan", selective_create)
    monkeypatch.setattr(MockVendor, "delete_vlan", lambda self, vlan_id, device, password: {"rc": 0, "stdout": "deleted", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 620, "name": "RBISO", "devices": ["rb_a", "rb_b"]})
    assert resp.status_code == 202
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['data']['group_job_id']}").json()["data"]

    results = {r["device"]: r for r in gj["device_results"]}

    assert results["rb_a"]["status"] == "failed"
    assert results["rb_a"]["rollback_performed"] is True

    assert results["rb_b"]["status"] == "completed"
    assert results["rb_b"]["rollback_performed"] is False
    assert results["rb_b"]["rollback_success"] is None


def test_rollback_failure_on_one_device_does_not_affect_another(client, monkeypatch):
    """Device 1 fails with a broken rollback. Device 2 still runs and succeeds."""
    _seed(("rb_c", "10.60.4.3"), ("rb_d", "10.60.4.4"))

    def selective_create(self, vlan_id, name, device, password):
        if device.name == "rb_c":
            return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(MockVendor, "create_vlan", selective_create)
    monkeypatch.setattr(MockVendor, "delete_vlan", lambda self, vlan_id, device, password: {"rc": 1, "stdout": "rb fail", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 621, "name": "RBFAILISO", "devices": ["rb_c", "rb_d"]})
    assert resp.status_code == 202
    gj = client.get(f"/api/v1/group-jobs/{resp.json()['data']['group_job_id']}").json()["data"]

    results = {r["device"]: r for r in gj["device_results"]}

    assert results["rb_c"]["rollback_performed"] is True
    assert results["rb_c"]["rollback_success"] is False

    assert results["rb_d"]["status"] == "completed"
    assert results["rb_d"]["rollback_performed"] is False


# ── 4. Lock correctness ───────────────────────────────────────────────────────


def test_lock_released_after_successful_job(client, monkeypatch):
    """Device lock must be released after a completed job — next job on same device runs."""
    _seed(("lck_a", "10.60.5.1"))

    monkeypatch.setattr(MockVendor, "create_vlan", lambda self, vlan_id, name, device, password: {"rc": 0, "stdout": "ok", "stderr": ""})

    r1 = client.post("/api/v1/vlans/", json={"vlan_id": 630, "name": "LCK1", "devices": ["lck_a"]})
    assert r1.status_code == 202

    assert not redis_coordinator.esta_ocupado("lck_a"), "Lock must be free after job completion"


def test_lock_released_after_failed_job(client, monkeypatch):
    """Device lock must be released even when the job fails — next job on same device is unblocked."""
    _seed(("lck_b", "10.60.5.2"))

    monkeypatch.setattr(
        MockVendor, "create_vlan",
        lambda self, vlan_id, name, device, password: {"rc": 1, "stdout": "", "stderr": "configuration syntax error"},
    )

    r1 = client.post("/api/v1/vlans/", json={"vlan_id": 631, "name": "LCK2", "devices": ["lck_b"]})
    assert r1.status_code == 202

    assert not redis_coordinator.esta_ocupado("lck_b"), "Lock must be free after job failure"

    # Confirm next operation on same device is not blocked
    monkeypatch.setattr(MockVendor, "create_vlan", lambda self, vlan_id, name, device, password: {"rc": 0, "stdout": "ok", "stderr": ""})
    r2 = client.post("/api/v1/vlans/", json={"vlan_id": 632, "name": "LCK3", "devices": ["lck_b"]})
    assert r2.status_code == 202
    gj2 = client.get(f"/api/v1/group-jobs/{r2.json()['data']['group_job_id']}").json()["data"]
    assert gj2["status"] == "completed"


def test_lock_released_after_rollback(client, monkeypatch):
    """Device lock must be released after rollback so subsequent jobs are not blocked."""
    _seed(("lck_c", "10.60.5.3"))

    monkeypatch.setattr(
        MockVendor, "create_vlan",
        lambda self, vlan_id, name, device, password: {"rc": 1, "stdout": "", "stderr": "configuration syntax error"},
    )
    monkeypatch.setattr(MockVendor, "delete_vlan", lambda self, vlan_id, device, password: {"rc": 0, "stdout": "ok", "stderr": ""})

    r1 = client.post("/api/v1/vlans/", json={"vlan_id": 633, "name": "LCK4", "devices": ["lck_c"]})
    assert r1.status_code == 202

    assert not redis_coordinator.esta_ocupado("lck_c"), "Lock must be free after rollback"


# ── 5. Observability quality ──────────────────────────────────────────────────
#
# The 3 log-based tests originally here ("group runner emits a terminal
# status log", "group runner logs a final outcome after the loop",
# "partial_success terminal log") asserted on logger
# app.services.group_job_service ("terminal status") and
# app.services.vlan_execution_service ("create complete") -- both modules
# are fully deleted (group jobs are computed on the fly by
# JobRepository.resumen_de_grupo(), no dedicated "terminal status" log line
# exists anywhere in the new pipeline; Orquestador's own log lines are
# per-attempt/per-retry, not a single per-group outcome line). No surviving
# code path reproduces either log message, so these 3 are deleted rather
# than repointed at unrelated text.


# ── 6. API clarity ────────────────────────────────────────────────────────────


def test_group_job_api_response_shape(client, monkeypatch):
    """GET /group-jobs/{id} must have all required top-level and summary fields."""
    monkeypatch.setattr(MockVendor, "create_vlan", lambda self, vlan_id, name, device, password: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 650, "name": "SHAPE", "devices": ["mock_device"]})
    assert resp.status_code == 202
    gj_resp = client.get(f"/api/v1/group-jobs/{resp.json()['data']['group_job_id']}").json()
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
    monkeypatch.setattr(MockVendor, "create_vlan", lambda self, vlan_id, name, device, password: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 651, "name": "JOBSHAPE", "devices": ["mock_device"]})
    assert resp.status_code == 202
    job_id = resp.json()["data"]["jobs"][0]["job_id"]

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
    monkeypatch.setattr(MockVendor, "create_vlan", lambda self, vlan_id, name, device, password: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 660, "name": "COMPAT", "devices": ["mock_device"]})
    assert resp.status_code == 202
    body = resp.json()

    assert body["success"] is True
    data = body["data"]
    assert isinstance(data["jobs"], list)
    assert len(data["jobs"]) == 1
    assert "group_job_id" in data
    job0 = data["jobs"][0]
    assert "job_id" in job0
    assert "device" in job0
    assert "status" in job0


def test_single_device_job_runs_and_completes(client, monkeypatch):
    """Single-device flow must still result in a completed job."""
    monkeypatch.setattr(MockVendor, "create_vlan", lambda self, vlan_id, name, device, password: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 661, "name": "COMPAT2", "devices": ["mock_device"]})
    assert resp.status_code == 202
    job_id = resp.json()["data"]["jobs"][0]["job_id"]
    job_resp = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job_resp["status"] == "completed"


def test_delete_single_device_backward_compat(client, monkeypatch):
    """Single-device delete must still return jobs list and group_job_id."""
    from app.models.vlan import VLAN

    monkeypatch.setattr(MockVendor, "get_vlans", lambda self, device, password: [VLAN(vlan_id=662, name="DCOMPAT")])
    monkeypatch.setattr(MockVendor, "delete_vlan", lambda self, vlan_id, device, password: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.request("DELETE", "/api/v1/vlans/662", json={"devices": ["mock_device"]})
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert "group_job_id" in data
    assert isinstance(data["jobs"], list)
    assert len(data["jobs"]) == 1


def test_update_single_device_backward_compat(client, monkeypatch):
    """Single-device update must still return jobs list and group_job_id."""
    monkeypatch.setattr(MockVendor, "update_vlan", lambda self, vlan_id, name, device, password: {"rc": 0, "stdout": "ok", "stderr": ""})

    resp = client.patch("/api/v1/vlans/10", json={"description": "Compat", "devices": ["mock_device"]})
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert "group_job_id" in data
    assert isinstance(data["jobs"], list)
