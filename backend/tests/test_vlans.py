"""VLAN create/delete/update API contract + validation (reserved IDs, name
format). Modernized from the pre-Repository[T]/Orquestador architecture:
``vlan_service`` is gone, VLAN writes go through ``VLAN`` domain objects +
``app.composition.group_operation_runner``, and mock-mode device behavior
(the "fail_device" convention) now lives on ``app.services.vendors.mock.MockVendor``
instead of a free-function service module.
"""
from app.composition import (
    device_repository,
    device_sync_service,
    inventory,
    plugin_registry,
    site_repository,
)
from app.core.exceptions import ValidationError as _ValidationError
from app.services.vendors.mock import MockVendor

# This repo's .env sets EXECUTION_MODE=real (a dev-environment override), so
# app.composition.build_plugin_registry() wires the real CiscoVendor/
# HuaweiVendor drivers (real Ansible playbooks) at import time. These tests
# were written against a simple, fully in-memory driver (canned
# get_vlans/create_vlan/delete_vlan/update_vlan results, "fail_device"
# simulates a failure) -- swapping the registry entries to MockVendor here
# restores that, while still exercising the real Orquestador/
# GroupOperationRunner/retry/rollback pipeline end-to-end. Safe at import
# time: Device.driver resolves the driver fresh via plugin_registry.obtener()
# every time a Device is loaded, so this takes effect before any device below
# is registered or used.
plugin_registry.registrar("cisco_ios", MockVendor())
plugin_registry.registrar("huawei_vrp", MockVendor())

_SITE_NAME = "VLAN Orchestration Test Site"


def _ensure_site(name: str = _SITE_NAME):
    try:
        return site_repository.crear_con_grupo_default(name, kind="REGULAR")
    except ValueError:
        return site_repository.list(name=name)[0]


def _ensure_device(name: str, vendor: str = "cisco_ios", host: "str | None" = None):
    """Idempotent device registration for tests -- replaces the dead
    device_service.create_device()/ValueError-on-duplicate pattern.
    All devices share one REGULAR-kind site created once at import time, so
    the conftest per-test fixture that grants operator/observer roles on
    every REGULAR site already covers it from the very first test."""
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
        # Populate the VLAN read cache so GET /vlans/?device=X has data
        # immediately -- sync_core() (the "all" scope Inventory.register()
        # fires) also touches ports/SVIs, which MockVendor doesn't
        # implement, so it raises before persisting anything.
        device_sync_service.sync_vlans(device)
    except Exception:
        pass
    return device


_ensure_device("mock_device")
_ensure_device("fail_device")


def test_create_vlan_success(client):
    payload = {"vlan_id": 800, "name": "TEST", "devices": ["mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 202
    data = response.json()["data"]
    assert len(data["jobs"]) == 1
    assert "job_id" in data["jobs"][0]
    assert data["jobs"][0]["status"] in ("pending", "running", "completed")


def test_vlan_invalid_id(client):
    payload = {"vlan_id": 5000, "name": "TEST", "devices": ["mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 422
    assert response.json()["details"]["errors"][0]["type"] == "less_than_equal"


def test_vlan_reserved(client):
    """VLAN(vlan_id=1, ...) raises during __post_init__/validar(), caught in
    the router and re-raised as ValidationError -- which the global handler
    now maps to 422 (not the 400 this test used to expect), see
    app/main.py's validation_error_handler."""
    payload = {"vlan_id": 1, "name": "TEST", "devices": ["mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 422
    assert "reserved" in response.text


def test_vlan_name_invalid(client):
    payload = {"vlan_id": 20, "name": "INVALID NAME", "devices": ["mock_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 422


def test_get_vlans(client):
    response = client.get("/api/v1/vlans/?device=mock_device")
    assert response.status_code == 200
    envelope = response.json()["data"]
    vlans = envelope["data"]
    assert isinstance(vlans, list)
    assert len(vlans) > 0
    assert "vlan_id" in vlans[0]
    assert "name" in vlans[0]


def test_delete_vlan_success(client):
    response = client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})
    assert response.status_code == 202
    data = response.json()["data"]
    assert len(data["jobs"]) == 1
    job = data["jobs"][0]
    assert "job_id" in job
    assert job["device"] == "mock_device"


def test_delete_vlan_invalid(client):
    response = client.request("DELETE", "/api/v1/vlans/1", json={"devices": ["mock_device"]})
    assert response.status_code == 422
    assert "reserved" in response.text


def test_update_vlan_reserved_id(client):
    payload = {"description": "Should be blocked", "devices": ["mock_device"]}
    response = client.patch("/api/v1/vlans/1", json=payload)
    assert response.status_code == 422
    assert "reserved" in response.text


def test_update_vlan_reserved_id_range(client):
    payload = {"description": "Should be blocked", "devices": ["mock_device"]}
    response = client.patch("/api/v1/vlans/1002", json=payload)
    assert response.status_code == 422
    assert "reserved" in response.text


def test_update_vlan_description(client):
    payload = {"description": "Core-network-VLAN", "devices": ["mock_device"]}
    response = client.patch("/api/v1/vlans/10", json=payload)
    assert response.status_code == 202
    data = response.json()["data"]
    assert len(data["jobs"]) == 1
    job = data["jobs"][0]
    assert "job_id" in job
    assert job["device"] == "mock_device"


def test_create_vlan_device_not_found(client):
    payload = {"vlan_id": 50, "name": "TEST", "devices": ["nonexistent_device"]}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 404
    assert "nonexistent_device" in response.text


def test_create_vlan_empty_devices_rejected(client):
    payload = {"vlan_id": 50, "name": "TEST", "devices": []}
    response = client.post("/api/v1/vlans/", json=payload)
    assert response.status_code == 422


def test_delete_vlan_failure(client):
    response = client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["fail_device"]})
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]
    response = client.get(f"/api/v1/jobs/{job_id}")
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert data["error"] is not None


def test_update_vlan_failure(client):
    payload = {"description": "Test-desc", "devices": ["fail_device"]}
    response = client.patch("/api/v1/vlans/10", json=payload)
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]
    response = client.get(f"/api/v1/jobs/{job_id}")
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert data["error"] is not None


def test_delete_nonexistent_vlan_job_completes_as_noop(client):
    """Deleting a VLAN that doesn't exist on a device is now an idempotent
    no-op success, not a failure -- see VLAN.aplicar()'s delete branch
    (``if not pre_state["existed"]: return {..., "noop": True}``), a
    deliberate behavior change from the old architecture this test used
    to assert (which treated a delete-of-absent as an error)."""
    response = client.request("DELETE", "/api/v1/vlans/99", json={"devices": ["mock_device"]})
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]
    response = client.get(f"/api/v1/jobs/{job_id}")
    data = response.json()["data"]
    assert data["status"] == "completed"
    assert data["result"]["noop"] is True


def test_delete_multi_device(client):
    """Delete on multiple devices creates one job per device."""
    response = client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device", "mock_device"]})
    assert response.status_code == 202
    data = response.json()["data"]
    assert len(data["jobs"]) == 2
    for job in data["jobs"]:
        assert "job_id" in job
        assert job["device"] == "mock_device"


def test_update_multi_device(client):
    """Update on multiple devices creates one job per device."""
    payload = {"description": "Multi-update", "devices": ["mock_device", "mock_device"]}
    response = client.patch("/api/v1/vlans/10", json=payload)
    assert response.status_code == 202
    data = response.json()["data"]
    assert len(data["jobs"]) == 2
    for job in data["jobs"]:
        assert "job_id" in job
        assert job["device"] == "mock_device"


def test_delete_valid_vlan_still_works(client):
    response = client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})
    assert response.status_code == 202
    data = response.json()["data"]
    assert len(data["jobs"]) == 1


# ── VLAN-003: get_vlans real-mode guard ───────────────────────────────────────

def test_get_vlans_no_device_real_mode_returns_400(client, monkeypatch):
    """GET /vlans/ without ?device= must fail when EXECUTION_MODE != mock.
    ``vlan_service`` is gone -- the router now reads
    ``app.core.config.EXECUTION_MODE`` fresh on every request, so patching
    that module attribute is the modern equivalent. Status is 422 now (the
    ValidationError handler), not the 400 this test used to expect."""
    monkeypatch.setattr("app.core.config.EXECUTION_MODE", "real")
    response = client.get("/api/v1/vlans/")
    assert response.status_code == 422
    assert "device" in response.json()["message"].lower()


def test_get_vlans_no_device_mock_mode_returns_list(client, monkeypatch):
    """GET /vlans/ without ?device= must still return the synthetic mock
    list when EXECUTION_MODE == mock."""
    monkeypatch.setattr("app.core.config.EXECUTION_MODE", "mock")
    response = client.get("/api/v1/vlans/")
    assert response.status_code == 200
    envelope = response.json()["data"]
    assert isinstance(envelope["data"], list)
    assert len(envelope["data"]) > 0


def test_get_vlans_with_device_returns_cached_data(client):
    """GET /vlans/?device=X returns the cached VLAN table for that device.
    No more live per-request driver call to capture (cache-first read
    model, see app/api/vlans.py:_leer_vlans_cache) -- the old test asserted
    on a captured device_id from a monkeypatched vlan_service.get_vlans(),
    which has no equivalent call site anymore."""
    response = client.get("/api/v1/vlans/?device=mock_device")
    assert response.status_code == 200
    envelope = response.json()["data"]
    assert isinstance(envelope["data"], list)
