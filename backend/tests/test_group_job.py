"""Tests for step 4.1: device group execution foundation."""
import time

import pytest

from app.services import group_job_service


# ── Domain model ──────────────────────────────────────────────────────────────

def test_device_execution_to_dict_round_trip():
    from app.models.group_job import DeviceExecution
    de = DeviceExecution(
        device="sw1",
        job_id="abc-123",
        status="completed",
        retry_count=1,
        rollback_performed=False,
        rollback_success=None,
        error=None,
        duration_ms=250,
    )
    assert DeviceExecution.from_dict(de.to_dict()) == de


def test_group_job_execution_summary_all_completed():
    from app.models.group_job import DeviceExecution, GroupJob
    gj = GroupJob(
        device_results=[
            DeviceExecution(device="sw1", status="completed"),
            DeviceExecution(device="sw2", status="completed"),
        ]
    )
    s = gj.execution_summary()
    assert s == {"total_devices": 2, "completed": 2, "failed": 0, "partial_success": False, "rollback_count": 0, "duration_ms": None}


def test_group_job_execution_summary_mixed():
    from app.models.group_job import DeviceExecution, GroupJob
    gj = GroupJob(
        device_results=[
            DeviceExecution(device="sw1", status="completed"),
            DeviceExecution(device="sw2", status="failed", rollback_performed=True),
        ]
    )
    s = gj.execution_summary()
    assert s == {"total_devices": 2, "completed": 1, "failed": 1, "partial_success": True, "rollback_count": 1, "duration_ms": None}


def test_group_job_execution_summary_all_failed():
    from app.models.group_job import DeviceExecution, GroupJob
    gj = GroupJob(
        device_results=[
            DeviceExecution(device="sw1", status="failed", rollback_performed=True),
            DeviceExecution(device="sw2", status="failed"),
        ]
    )
    s = gj.execution_summary()
    assert s == {"total_devices": 2, "completed": 0, "failed": 2, "partial_success": False, "rollback_count": 1, "duration_ms": None}


# ── Service: create + retrieve ────────────────────────────────────────────────

def test_create_and_get_group_job():
    gj = group_job_service.create_group_job(
        operation="create_vlan",
        playbook="create_vlan.yml",
        parameters={"vlan_id": 300, "name": "TEST"},
        devices=["sw1", "sw2"],
    )
    assert gj.group_job_id
    assert gj.status == "pending"
    assert gj.total_devices == 2
    assert {r.device for r in gj.device_results} == {"sw1", "sw2"}
    assert all(r.status == "pending" for r in gj.device_results)

    fetched = group_job_service.get_group_job(gj.group_job_id)
    assert fetched is not None
    assert fetched.group_job_id == gj.group_job_id
    assert fetched.operation == "create_vlan"
    assert fetched.total_devices == 2


def test_get_group_job_not_found_returns_none():
    assert group_job_service.get_group_job("nonexistent-id") is None


# ── Service: status aggregation ───────────────────────────────────────────────

def test_update_device_result_sets_running_while_partial():
    gj = group_job_service.create_group_job(
        operation="create_vlan",
        playbook="create_vlan.yml",
        parameters={"vlan_id": 301},
        devices=["sw1", "sw2"],
    )
    group_job_service.update_device_result(
        gj.group_job_id, device="sw1", job_id="j1", status="completed",
    )
    fetched = group_job_service.get_group_job(gj.group_job_id)
    assert fetched.status == "running"


def test_status_aggregation_all_completed():
    gj = group_job_service.create_group_job(
        operation="create_vlan",
        playbook="create_vlan.yml",
        parameters={"vlan_id": 302},
        devices=["sw1", "sw2"],
    )
    group_job_service.update_device_result(gj.group_job_id, "sw1", "j1", "completed")
    group_job_service.update_device_result(gj.group_job_id, "sw2", "j2", "completed")
    fetched = group_job_service.get_group_job(gj.group_job_id)
    assert fetched.status == "completed"
    assert fetched.finished_at is not None


def test_status_aggregation_all_failed():
    gj = group_job_service.create_group_job(
        operation="create_vlan",
        playbook="create_vlan.yml",
        parameters={"vlan_id": 303},
        devices=["sw1", "sw2"],
    )
    group_job_service.update_device_result(gj.group_job_id, "sw1", "j1", "failed")
    group_job_service.update_device_result(gj.group_job_id, "sw2", "j2", "failed")
    fetched = group_job_service.get_group_job(gj.group_job_id)
    assert fetched.status == "failed"


def test_status_aggregation_partial_success():
    gj = group_job_service.create_group_job(
        operation="create_vlan",
        playbook="create_vlan.yml",
        parameters={"vlan_id": 304},
        devices=["sw1", "sw2"],
    )
    group_job_service.update_device_result(gj.group_job_id, "sw1", "j1", "completed")
    group_job_service.update_device_result(gj.group_job_id, "sw2", "j2", "failed")
    fetched = group_job_service.get_group_job(gj.group_job_id)
    assert fetched.status == "partial_success"


def test_update_device_result_stores_all_fields():
    gj = group_job_service.create_group_job(
        operation="delete_vlan",
        playbook="delete_vlan.yml",
        parameters={"vlan_id": 305},
        devices=["sw1"],
    )
    group_job_service.update_device_result(
        gj.group_job_id,
        device="sw1",
        job_id="job-xyz",
        status="failed",
        current_step="rollback_completed",
        retry_count=2,
        rollback_performed=True,
        rollback_success=True,
        error="Timeout",
        duration_ms=1500,
    )
    fetched = group_job_service.get_group_job(gj.group_job_id)
    r = fetched.device_results[0]
    assert r.job_id == "job-xyz"
    assert r.status == "failed"
    assert r.current_step == "rollback_completed"
    assert r.retry_count == 2
    assert r.rollback_performed is True
    assert r.rollback_success is True
    assert r.error == "Timeout"
    assert r.duration_ms == 1500


# ── API: VLAN operations return group_job_id ──────────────────────────────────

def test_create_vlan_response_includes_group_job_id(client):
    payload = {"vlan_id": 310, "name": "GRPTEST", "devices": ["mock_device"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "group_job_id" in data
    assert data["group_job_id"] is not None
    assert len(data["jobs"]) == 1


def test_create_vlan_multi_device_group_job_id(client):
    from app.services import device_service
    for name, host in [("grp_dev1", "10.99.1.1"), ("grp_dev2", "10.99.1.2")]:
        try:
            device_service.create_device(name, host, "cisco_ios", "admin", "pass")
        except ValueError:
            pass
    payload = {"vlan_id": 311, "name": "GRPMULTI", "devices": ["grp_dev1", "grp_dev2"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert "group_job_id" in data
    assert len(data["jobs"]) == 2


def test_delete_vlan_response_includes_group_job_id(client):
    # Create first so there's something to delete
    client.post("/api/v1/vlans/", json={"vlan_id": 312, "name": "DELGRP", "devices": ["mock_device"]})
    time.sleep(0.2)
    resp = client.request("DELETE", "/api/v1/vlans/312", json={"devices": ["mock_device"]})
    assert resp.status_code == 200
    data = resp.json()
    assert "group_job_id" in data
    assert data["group_job_id"] is not None


def test_update_vlan_response_includes_group_job_id(client):
    client.post("/api/v1/vlans/", json={"vlan_id": 313, "name": "UPDGRP", "devices": ["mock_device"]})
    time.sleep(0.2)
    resp = client.patch("/api/v1/vlans/313", json={"description": "Updated", "devices": ["mock_device"]})
    assert resp.status_code == 200
    data = resp.json()
    assert "group_job_id" in data
    assert data["group_job_id"] is not None


# ── API: GET /group-jobs/{id} ─────────────────────────────────────────────────

def test_get_group_job_endpoint(client):
    payload = {"vlan_id": 320, "name": "GETGRP", "devices": ["mock_device"]}
    create_resp = client.post("/api/v1/vlans/", json=payload)
    assert create_resp.status_code == 200
    group_job_id = create_resp.json()["group_job_id"]

    time.sleep(0.3)

    resp = client.get(f"/api/v1/group-jobs/{group_job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    gj = data["data"]
    assert gj["group_job_id"] == group_job_id
    assert gj["operation"] == "create_vlan"
    assert "execution_summary" in gj
    assert "device_results" in gj
    assert len(gj["device_results"]) == 1
    s = gj["execution_summary"]
    assert "total_devices" in s
    assert "completed" in s
    assert "failed" in s
    assert "rollback_count" in s


def test_get_group_job_not_found(client):
    resp = client.get("/api/v1/group-jobs/does-not-exist")
    assert resp.status_code == 404


def test_get_group_job_unauthenticated(unauth_client):
    resp = unauth_client.get("/api/v1/group-jobs/anything")
    assert resp.status_code == 401


# ── API: per-device job has group_job_id ─────────────────────────────────────

def test_job_response_includes_group_job_id(client):
    payload = {"vlan_id": 330, "name": "JOBGRP", "devices": ["mock_device"]}
    create_resp = client.post("/api/v1/vlans/", json=payload)
    assert create_resp.status_code == 200
    job_id = create_resp.json()["jobs"][0]["job_id"]
    group_job_id = create_resp.json()["group_job_id"]

    time.sleep(0.3)

    resp = client.get(f"/api/v1/jobs/{job_id}")
    assert resp.status_code == 200
    job_data = resp.json()["data"]
    assert job_data["group_job_id"] == group_job_id


# ── API: group job device_results populated after execution ───────────────────

def test_group_job_device_results_populated_after_execution(client):
    payload = {"vlan_id": 340, "name": "EXECGRP", "devices": ["mock_device"]}
    create_resp = client.post("/api/v1/vlans/", json=payload)
    group_job_id = create_resp.json()["group_job_id"]

    time.sleep(0.5)

    resp = client.get(f"/api/v1/group-jobs/{group_job_id}")
    gj = resp.json()["data"]
    assert gj["status"] in ("completed", "failed", "partial_success")
    assert len(gj["device_results"]) == 1
    dr = gj["device_results"][0]
    assert dr["device"] == "mock_device"
    assert dr["job_id"] is not None
    assert dr["status"] in ("completed", "failed")


def test_group_job_multi_device_all_completed_aggregation(client):
    from app.services import device_service
    for name, host in [("agg_dev1", "10.99.2.1"), ("agg_dev2", "10.99.2.2")]:
        try:
            device_service.create_device(name, host, "cisco_ios", "admin", "pass")
        except ValueError:
            pass
    payload = {"vlan_id": 341, "name": "MULTAGG", "devices": ["agg_dev1", "agg_dev2"]}
    create_resp = client.post("/api/v1/vlans/", json=payload)
    group_job_id = create_resp.json()["group_job_id"]

    time.sleep(0.7)

    resp = client.get(f"/api/v1/group-jobs/{group_job_id}")
    gj = resp.json()["data"]
    assert gj["status"] in ("completed", "failed", "partial_success")
    s = gj["execution_summary"]
    assert s["total_devices"] == 2
    assert s["completed"] + s["failed"] == 2


# ── Backward compatibility: single-device flow ────────────────────────────────

def test_backward_compat_single_device_create(client):
    """Single-device create still works; response gains group_job_id additively."""
    payload = {"vlan_id": 350, "name": "COMPAT", "devices": ["mock_device"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert isinstance(data["jobs"], list)
    assert len(data["jobs"]) == 1
    assert "job_id" in data["jobs"][0]
    assert "group_job_id" in data
