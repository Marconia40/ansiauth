"""API-level tests for group-job tracking (group_job_id on VLAN writes,
GET /group-jobs/{id}). The unit tests that used to live here tested
GroupJob/DeviceExecution/group_job_service directly -- all 3 were deleted
by the migration to FINAL_ARCHITECTURE.md (no DB row for a group job
anymore; JobRepository.resumen_de_grupo() computes the same aggregation
on the fly from the real per-device Job rows). Deleted rather than
ported -- there's no 1:1 successor to unit-test, and the aggregation
logic itself is exercised end-to-end by the API tests below.

Two behavior changes from the pre-migration version of this file, found
live: every write now responds 202 (not 200), and every response body is
wrapped in the standard {"success": bool, "data": {...}} envelope (was
flat)."""
import time

import pytest


@pytest.fixture(scope="session", autouse=True)
def _seed_mock_devices():
    """Register "mock_device" once for this file's session -- device_service.py
    (and its seed_defaults()/"mock_device" special-casing) was deleted;
    Inventory.register() needs a real, persisted Device row (require_device()
    404s otherwise, before any authz check runs). Same pattern as
    test_jobs.py's fixture of the same name."""
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
    the duration of each test -- same pattern as test_jobs.py."""
    from app.composition import plugin_registry
    from app.services.vendors.mock import MockVendor

    mock = MockVendor()
    for vendor in ("cisco_ios", "huawei_vrp"):
        monkeypatch.setitem(plugin_registry._vendors, vendor, mock)


# ── API: VLAN operations return group_job_id ──────────────────────────────────

def test_create_vlan_response_includes_group_job_id(client):
    payload = {"vlan_id": 310, "name": "GRPTEST", "devices": ["mock_device"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert "group_job_id" in data
    assert data["group_job_id"] is not None
    assert len(data["jobs"]) == 1


def test_create_vlan_multi_device_group_job_id(client):
    from app.composition import inventory, site_repository
    from app.core.exceptions import ValidationError

    site = site_repository.crear_con_grupo_default("Group Job Test Site A", kind="REGULAR")
    for name, host in [("grp_dev1", "10.99.1.1"), ("grp_dev2", "10.99.1.2")]:
        try:
            inventory.register(
                name=name, host=host, vendor="cisco_ios", platform="ios",
                username="admin", password="pass",
                site_id=site.id, device_group_id=site.default_group_id,
                actor={"username": "admin"},
            )
        except ValidationError:
            pass
    payload = {"vlan_id": 311, "name": "GRPMULTI", "devices": ["grp_dev1", "grp_dev2"]}
    resp = client.post("/api/v1/vlans/", json=payload)
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert "group_job_id" in data
    assert len(data["jobs"]) == 2


def test_delete_vlan_response_includes_group_job_id(client):
    # Create first so there's something to delete
    client.post("/api/v1/vlans/", json={"vlan_id": 312, "name": "DELGRP", "devices": ["mock_device"]})
    time.sleep(0.2)
    resp = client.request("DELETE", "/api/v1/vlans/312", json={"devices": ["mock_device"]})
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert "group_job_id" in data
    assert data["group_job_id"] is not None


def test_update_vlan_response_includes_group_job_id(client):
    client.post("/api/v1/vlans/", json={"vlan_id": 313, "name": "UPDGRP", "devices": ["mock_device"]})
    time.sleep(0.2)
    resp = client.patch("/api/v1/vlans/313", json={"description": "Updated", "devices": ["mock_device"]})
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert "group_job_id" in data
    assert data["group_job_id"] is not None


# ── API: GET /group-jobs/{id} ─────────────────────────────────────────────────

def test_get_group_job_endpoint(client):
    payload = {"vlan_id": 320, "name": "GETGRP", "devices": ["mock_device"]}
    create_resp = client.post("/api/v1/vlans/", json=payload)
    assert create_resp.status_code == 202
    group_job_id = create_resp.json()["data"]["group_job_id"]

    time.sleep(0.3)

    resp = client.get(f"/api/v1/group-jobs/{group_job_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    gj = data["data"]
    assert gj["group_job_id"] == group_job_id
    assert gj["operation"] == "vlan"  # Job.operation is the resource type ("vlan"), not the verb
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
    assert create_resp.status_code == 202
    created = create_resp.json()["data"]
    job_id = created["jobs"][0]["job_id"]
    group_job_id = created["group_job_id"]

    time.sleep(0.3)

    resp = client.get(f"/api/v1/jobs/{job_id}")
    assert resp.status_code == 200
    job_data = resp.json()["data"]
    assert job_data["group_job_id"] == group_job_id


# ── API: group job device_results populated after execution ───────────────────

def test_group_job_device_results_populated_after_execution(client):
    payload = {"vlan_id": 340, "name": "EXECGRP", "devices": ["mock_device"]}
    create_resp = client.post("/api/v1/vlans/", json=payload)
    group_job_id = create_resp.json()["data"]["group_job_id"]

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
    from app.composition import inventory, site_repository
    from app.core.exceptions import ValidationError

    site = site_repository.crear_con_grupo_default("Group Job Test Site B", kind="REGULAR")
    for name, host in [("agg_dev1", "10.99.2.1"), ("agg_dev2", "10.99.2.2")]:
        try:
            inventory.register(
                name=name, host=host, vendor="cisco_ios", platform="ios",
                username="admin", password="pass",
                site_id=site.id, device_group_id=site.default_group_id,
                actor={"username": "admin"},
            )
        except ValidationError:
            pass
    payload = {"vlan_id": 341, "name": "MULTAGG", "devices": ["agg_dev1", "agg_dev2"]}
    create_resp = client.post("/api/v1/vlans/", json=payload)
    group_job_id = create_resp.json()["data"]["group_job_id"]

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
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert resp.json()["success"] is True
    assert isinstance(data["jobs"], list)
    assert len(data["jobs"]) == 1
    assert "job_id" in data["jobs"][0]
    assert "group_job_id" in data
