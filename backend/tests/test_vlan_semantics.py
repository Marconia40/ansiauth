"""Tests for create/update VLAN operation semantics (existence validation).

Ported from the pre-Repository[T]/Orquestador architecture. Several of the
original scenarios have no modern equivalent -- ``VLAN.aplicar()`` (Fase 2/5)
fused what used to be 3 separate functions (create_vlan/update_vlan/delete_vlan,
each with its own validation) into ONE dispatch driven entirely by
``pre_state`` (see app/models/vlan.py):

    if existed and name == self.name:  no-op
    if existed and name != self.name:  UPDATE (rename) -- for BOTH the
                                        create (POST) and update (PATCH) routes
    if not existed:                    CREATE -- for BOTH routes too

So "create must fail when the VLAN already exists with a different name" and
"update must fail when the VLAN does not exist" are gone as hard errors --
POST and PATCH now converge on the same idempotent upsert, dispatched purely
by device pre-state. Those tests are deleted below (not weakened) since there
is no code path left that reproduces the old failure. This looks like a
deliberate simplification (the class docstring frames create/update/delete as
one fused operation), not an accidental regression -- flagged in the review
report regardless, since it is a real product-behavior change from the old
system's explicit "duplicate VLAN" rejection.

The ``_capture_pre_state_vlan`` "existed: None / unknown state" abort
mechanism from api/vlans.py is also gone entirely -- ``VLAN.reconciliar()``
always returns a concrete boolean (``next(..., None) is not None``), never an
"unknown" tri-state; a genuine read failure now surfaces as an exception that
Orquestador's retry/rollback path handles like any other device error, not as
a distinct precheck-failed status. The 4 tests exercising that mechanism are
deleted for the same reason -- no surviving code path to port them to.
"""
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
from app.models.vlan import VLAN
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
    """AuditRecord.job_id is never populated for VLAN/resource operations
    dispatched via Orquestador's generic DomainEvent -> AuditListener path
    (AuditRecord.desde() only fills user/action/resource/summary/device/
    status -- see app/models/audit.py/app/services/audit_listener.py) --
    the old ``entry.job_id == job_id`` correlation the original tests used
    has no equivalent anymore. Correlate on device + the human-readable
    summary instead (``resumen_intento()`` embeds the vlan_id), taking the
    most recent matching row (query() already orders by timestamp desc)."""
    records, _total = audit_repository.query(
        scope=_ADMIN_SCOPE, device_id=device, page=1, page_size=20,
    )
    if vlan_id is not None:
        records = [r for r in records if f"VLAN {vlan_id}" in (r.summary or "")]
    return records[0]


# ── create_vlan ───────────────────────────────────────────────────────────────

def test_create_existing_vlan_same_name_noop(operator_client, client, monkeypatch):
    """create_vlan must succeed as a no-op when the VLAN already has the
    requested name."""
    monkeypatch.setattr(
        MockVendor, "get_vlans",
        lambda self, device, password: [VLAN(vlan_id=50, name="MGMT50")],
    )

    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 50, "name": "MGMT50", "devices": ["mock_device"]}
    )
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    assert job["result"]["noop"] is True

    entry = _latest_audit_entry(vlan_id=50)
    assert entry.status == "success"
    assert "no changes needed" in (entry.summary or "")


def test_create_new_vlan_succeeds(operator_client, client):
    """create_vlan must succeed when VLAN does not exist on the device."""
    response = operator_client.post(
        "/api/v1/vlans/", json={"vlan_id": 800, "name": "NEW", "devices": ["mock_device"]}
    )
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"


# ── update_vlan ───────────────────────────────────────────────────────────────

def test_update_existing_vlan_succeeds(operator_client, client):
    """update_vlan must succeed when VLAN exists on the device."""
    # VLAN 10 exists in mock_vlans
    response = operator_client.patch(
        "/api/v1/vlans/10", json={"description": "Updated-name", "devices": ["mock_device"]}
    )
    assert response.status_code == 202
    job_id = response.json()["data"]["jobs"][0]["job_id"]

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
