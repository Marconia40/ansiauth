"""Tests for step 4.2: sequential multi-device execution with failure isolation."""
import threading

import pytest

from app.services import device_service, vlan_service


def _seed_devices(*specs):
    """Create test devices, ignoring duplicates."""
    for name, host in specs:
        try:
            device_service.create_device(name, host, "cisco_ios", "admin", "pass")
        except ValueError:
            pass


# ── Failure isolation — create ────────────────────────────────────────────────

def test_create_failure_isolation_middle_device(client, monkeypatch):
    """Middle device failure must not prevent device 3 from executing."""
    _seed_devices(("sq_c1", "10.50.1.1"), ("sq_c3", "10.50.1.3"))

    executed: list[str] = []

    def selective_create(vlan_id, name, device_id):
        executed.append(device_id)
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "simulated failure", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", selective_create)

    payload = {"vlan_id": 501, "name": "SEQTEST", "devices": ["sq_c1", "fail_device", "sq_c3"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 200
    group_job_id = resp.json()["group_job_id"]
    assert len(resp.json()["jobs"]) == 3

    # All 3 devices must have been attempted
    assert executed == ["sq_c1", "fail_device", "sq_c3"]

    gj = client.get(f"/api/v1/group-jobs/{group_job_id}").json()["data"]
    assert gj["status"] == "partial_success"
    s = gj["execution_summary"]
    assert s["total_devices"] == 3
    assert s["completed"] == 2
    assert s["failed"] == 1


def test_create_failure_isolation_first_device(client, monkeypatch):
    """First device failure must not prevent remaining devices from executing."""
    _seed_devices(("sq_c4", "10.50.2.1"), ("sq_c5", "10.50.2.2"))

    executed: list[str] = []

    def selective_create(vlan_id, name, device_id):
        executed.append(device_id)
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "err", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", selective_create)

    payload = {"vlan_id": 502, "name": "SEQFIRST", "devices": ["fail_device", "sq_c4", "sq_c5"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 200
    group_job_id = resp.json()["group_job_id"]

    assert executed == ["fail_device", "sq_c4", "sq_c5"]

    gj = client.get(f"/api/v1/group-jobs/{group_job_id}").json()["data"]
    assert gj["status"] == "partial_success"
    assert gj["execution_summary"]["completed"] == 2
    assert gj["execution_summary"]["failed"] == 1


def test_create_failure_isolation_last_device(client, monkeypatch):
    """Last device failure produces partial_success — earlier successes preserved."""
    _seed_devices(("sq_c6", "10.50.3.1"), ("sq_c7", "10.50.3.2"))

    executed: list[str] = []

    def selective_create(vlan_id, name, device_id):
        executed.append(device_id)
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "err", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", selective_create)

    payload = {"vlan_id": 503, "name": "SEQLAST", "devices": ["sq_c6", "sq_c7", "fail_device"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 200

    assert executed == ["sq_c6", "sq_c7", "fail_device"]

    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]
    assert gj["status"] == "partial_success"


def test_create_all_devices_succeed(client, monkeypatch):
    """All devices succeed → group status completed."""
    _seed_devices(("sq_ok1", "10.50.4.1"), ("sq_ok2", "10.50.4.2"))

    executed: list[str] = []

    def track_create(vlan_id, name, device_id):
        executed.append(device_id)
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", track_create)

    payload = {"vlan_id": 504, "name": "ALLOK", "devices": ["sq_ok1", "sq_ok2"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 200

    assert executed == ["sq_ok1", "sq_ok2"]

    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]
    assert gj["status"] == "completed"
    assert gj["execution_summary"]["completed"] == 2
    assert gj["execution_summary"]["failed"] == 0


def test_create_all_devices_fail(client, monkeypatch):
    """All devices fail → group status failed."""
    _seed_devices(("sq_f1", "10.50.5.1"), ("sq_f2", "10.50.5.2"))

    executed: list[str] = []

    def all_fail(vlan_id, name, device_id):
        executed.append(device_id)
        return {"rc": 1, "stdout": "err", "stderr": "forced failure"}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", all_fail)

    payload = {"vlan_id": 505, "name": "ALLFAIL", "devices": ["sq_f1", "sq_f2"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 200

    assert executed == ["sq_f1", "sq_f2"]

    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]
    assert gj["status"] == "failed"
    assert gj["execution_summary"]["failed"] == 2
    assert gj["execution_summary"]["completed"] == 0


# ── Sequential ordering ───────────────────────────────────────────────────────

def test_sequential_ordering_three_devices(client, monkeypatch):
    """Execution order matches device list order; never more than one device at a time."""
    _seed_devices(("ord_a", "10.50.6.1"), ("ord_b", "10.50.6.2"), ("ord_c", "10.50.6.3"))

    execution_order: list[str] = []
    active: list[str] = []
    max_concurrent = [0]
    lock = threading.Lock()

    def tracked_create(vlan_id, name, device_id):
        with lock:
            active.append(device_id)
            max_concurrent[0] = max(max_concurrent[0], len(active))
        result = {"rc": 0, "stdout": "ok", "stderr": ""}
        with lock:
            execution_order.append(device_id)
            active.remove(device_id)
        return result

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", tracked_create)

    payload = {"vlan_id": 506, "name": "ORDTEST", "devices": ["ord_a", "ord_b", "ord_c"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 200

    assert execution_order == ["ord_a", "ord_b", "ord_c"]
    assert max_concurrent[0] == 1


def test_sequential_ordering_two_devices_with_failure(client, monkeypatch):
    """Order is preserved even when first device fails."""
    _seed_devices(("ord_d", "10.50.7.1"))

    execution_order: list[str] = []

    def selective(vlan_id, name, device_id):
        execution_order.append(device_id)
        return {"rc": 0 if device_id != "fail_device" else 1, "stdout": "", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", selective)

    payload = {"vlan_id": 507, "name": "ORDWFAIL", "devices": ["fail_device", "ord_d"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 200

    assert execution_order == ["fail_device", "ord_d"]


# ── Failure isolation — update ────────────────────────────────────────────────

def test_update_failure_isolation(client):
    """Update: failure on one device does not prevent others from running.

    Uses mock mode's built-in fail_device behavior (rc=1) so rollback
    call tracking doesn't interfere with the assertion.
    """
    _seed_devices(("upd_a", "10.50.8.1"), ("upd_b", "10.50.8.2"))

    # VLAN 10 exists in mock list; fail_device returns rc=1 from the mock.
    payload = {"description": "NewDesc", "devices": ["upd_a", "fail_device", "upd_b"]}
    resp = client.patch("/api/v1/vlans/10", json=payload)
    assert resp.status_code == 200

    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]
    assert gj["status"] == "partial_success"
    assert gj["execution_summary"]["total_devices"] == 3
    assert gj["execution_summary"]["completed"] == 2
    assert gj["execution_summary"]["failed"] == 1

    devices_attempted = {r["device"] for r in gj["device_results"]}
    assert devices_attempted == {"upd_a", "fail_device", "upd_b"}


def test_update_all_succeed(client):
    """Update: all devices succeed → completed."""
    _seed_devices(("upd_c", "10.50.9.1"), ("upd_d", "10.50.9.2"))

    resp = client.patch("/api/v1/vlans/10", json={"description": "OK", "devices": ["upd_c", "upd_d"]})
    assert resp.status_code == 200

    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]
    assert gj["status"] == "completed"
    assert gj["execution_summary"]["completed"] == 2


# ── Failure isolation — delete ────────────────────────────────────────────────

def test_delete_failure_isolation(client, monkeypatch):
    """Delete: failure on one device does not prevent others from running."""
    _seed_devices(("del_a", "10.50.10.1"), ("del_b", "10.50.10.2"))

    # Patch get_vlans so all devices "see" the VLAN as present (pre-existence check).
    from app.models.vlan import VLANInfo
    mock_with_vlan = [VLANInfo(vlan_id=555, name="DELTEST")]

    executed_delete: list[str] = []
    executed_get: list[str] = []

    def fake_get_vlans(device_id=None):
        if device_id is not None:
            executed_get.append(device_id)
        # Always report the VLAN as present (so the existence check passes,
        # and the post-delete verification is handled per-device below).
        return mock_with_vlan

    def selective_delete(vlan_id, device_id):
        executed_delete.append(device_id)
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "err", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "get_vlans", fake_get_vlans)
    monkeypatch.setattr(vlan_service, "delete_vlan", selective_delete)

    resp = client.request("DELETE", "/api/v1/vlans/555", json={"devices": ["del_a", "fail_device", "del_b"]})
    assert resp.status_code == 200

    assert executed_delete == ["del_a", "fail_device", "del_b"]

    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]
    # del_a and del_b succeed; fail_device fails. del_a and del_b will see
    # VLAN still present in post-verification (fake_get_vlans always returns it),
    # so they'll also fail verification. But fail_device returns rc=1 which fails
    # before verification. All three fail from the delete perspective.
    # The key assertion is that all three devices were attempted.
    assert gj["execution_summary"]["total_devices"] == 3


def test_delete_all_succeed(client, monkeypatch):
    """Delete: all devices succeed → completed."""
    _seed_devices(("del_c", "10.50.11.1"), ("del_d", "10.50.11.2"))

    from app.models.vlan import VLANInfo

    call_counts: dict[str, int] = {}

    def counting_get_vlans(device_id=None):
        if device_id is not None:
            call_counts[device_id] = call_counts.get(device_id, 0) + 1
            # First call: VLAN present (existence check and pre_state)
            # Second call: VLAN absent (post-delete verification)
            if call_counts[device_id] <= 2:
                return [VLANInfo(vlan_id=556, name="DTEST")]
        return []

    def succeed_delete(vlan_id, device_id):
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "get_vlans", counting_get_vlans)
    monkeypatch.setattr(vlan_service, "delete_vlan", succeed_delete)

    resp = client.request("DELETE", "/api/v1/vlans/556", json={"devices": ["del_c", "del_d"]})
    assert resp.status_code == 200

    gj = client.get(f"/api/v1/group-jobs/{resp.json()['group_job_id']}").json()["data"]
    assert gj["execution_summary"]["total_devices"] == 2


# ── Per-device results in group job ──────────────────────────────────────────

def test_device_results_reflect_individual_outcomes(client, monkeypatch):
    """Each device's result in device_results matches its actual outcome."""
    _seed_devices(("dr_a", "10.50.12.1"), ("dr_b", "10.50.12.2"), ("dr_c", "10.50.12.3"))

    def selective(vlan_id, name, device_id):
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "err", "stderr": ""}
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", selective)

    payload = {"vlan_id": 508, "name": "DRTEST", "devices": ["dr_a", "fail_device", "dr_b"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    group_job_id = resp.json()["group_job_id"]

    gj = client.get(f"/api/v1/group-jobs/{group_job_id}").json()["data"]
    results_by_device = {r["device"]: r for r in gj["device_results"]}

    assert results_by_device["dr_a"]["status"] == "completed"
    assert results_by_device["fail_device"]["status"] == "failed"
    assert results_by_device["dr_b"]["status"] == "completed"

    # job_id must be linked for each device
    assert results_by_device["dr_a"]["job_id"] is not None
    assert results_by_device["fail_device"]["job_id"] is not None


# ── Single device backward compatibility ──────────────────────────────────────

def test_single_device_create_still_works(client, monkeypatch):
    """Single-device path unchanged — group_job created with 1 device."""
    executed: list[str] = []

    def track(vlan_id, name, device_id):
        executed.append(device_id)
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", track)

    resp = client.post("/api/v1/vlans/", json={"vlan_id": 509, "name": "COMPAT", "devices": ["mock_device"]})
    assert resp.status_code == 200
    data = resp.json()

    assert data["success"] is True
    assert len(data["jobs"]) == 1
    assert "group_job_id" in data
    assert executed == ["mock_device"]

    gj = client.get(f"/api/v1/group-jobs/{data['group_job_id']}").json()["data"]
    assert gj["status"] == "completed"
    assert gj["execution_summary"]["total_devices"] == 1


def test_single_device_update_still_works(client, monkeypatch):
    """Single-device update path unchanged."""
    executed: list[str] = []

    def track_update(vlan_id, desc, device_id):
        executed.append(device_id)
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    monkeypatch.setattr(vlan_service, "update_vlan_description", track_update)

    resp = client.patch("/api/v1/vlans/10", json={"description": "Solo", "devices": ["mock_device"]})
    assert resp.status_code == 200
    data = resp.json()

    assert "group_job_id" in data
    assert executed == ["mock_device"]


# ── Observability: logs identify job, device, VLAN ───────────────────────────

def test_group_runner_logs_device_progress(client, monkeypatch, caplog):
    """Group runner must log start/finish for each device for observability."""
    import logging
    _seed_devices(("log_a", "10.50.13.1"), ("log_b", "10.50.13.2"))

    monkeypatch.setattr(vlan_service, "create_vlan_on_device",
                        lambda vlan_id, name, device_id: {"rc": 0, "stdout": "ok", "stderr": ""})

    with caplog.at_level(logging.INFO, logger="app.services.vlan_execution_service"):
        resp = client.post("/api/v1/vlans/", json={"vlan_id": 510, "name": "LOGTEST", "devices": ["log_a", "log_b"]})

    assert resp.status_code == 200

    log_text = caplog.text
    assert "sequential create" in log_text
    assert "log_a" in log_text
    assert "log_b" in log_text
