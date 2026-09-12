"""API-level tests for ``GET /api/v1/devices/{name}/ports/`` (Step 1.2).

Modernized against the current architecture: the endpoint is cache-first
now -- it reads ``app.composition.puerto_repository`` (populated by
``device_sync_service``, not a live Ansible/driver call at request time)
and wraps the payload in a ``SyncedResource`` envelope
(``synced_at``/``sync_error``/``sync_in_progress``). There is no more
``port_service``/live-EXECUTION_MODE branching to mock here -- tests seed
``puerto_repository`` directly instead.

Response shape: ``ok(SyncedResource(...).model_dump())`` -- note the
DOUBLE ``"data"`` nesting: the outer envelope's ``data`` holds the
``SyncedResource``, whose OWN ``data`` field holds
``{"device", "vendor", "count", "ports"}``. So
``res.json()["data"]["data"]["ports"]``.

``PortRead`` (``app/schemas/port.py``) no longer exposes
``poe_enabled``/``speed``/``duplex`` on the wire at all (confirmed by
reading the schema) -- those fields existed on the old ``PortInfo``
model but were dropped from the current read-side contract. The old
"every documented field present" test is adjusted to the real current
field set.
"""

from __future__ import annotations

import pytest

from app.composition import puerto_repository
from app.models.port import Puerto

from tests._puerto_fakes import get_or_create_device

_DEVICE = "list_dev"


@pytest.fixture
def device():
    return get_or_create_device(_DEVICE, site_name="Ports List Test Site")


@pytest.fixture
def seeded_ports(device):
    ports = [
        Puerto(
            interface="GigabitEthernet0/0/1", device=_DEVICE, description="Workstation-01",
            admin_up=True, operational_up=True, mode="access", access_vlan=10,
        ),
        Puerto(
            interface="GigabitEthernet0/0/24", device=_DEVICE, description="Uplink",
            admin_up=True, operational_up=True, mode="trunk", access_vlan=1,
            allowed_vlans=[10, 20, 30],
        ),
    ]
    for p in ports:
        puerto_repository.add(p)
    return ports


def _url(name: str = _DEVICE) -> str:
    return f"/api/v1/devices/{name}/ports/"


# -- Happy path (cache-first) --------------------------------------------------

def test_list_ports_returns_envelope(client, seeded_ports):
    res = client.get(_url())
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    payload = body["data"]["data"]
    assert payload["device"] == _DEVICE
    assert payload["count"] == len(payload["ports"])
    assert payload["count"] == 2
    assert isinstance(payload["ports"], list)
    for port in payload["ports"]:
        for key in (
            "name", "description", "admin_up", "operational_up", "mode",
            "access_vlan", "allowed_vlans", "storm_control_enabled",
            "storm_control_threshold", "storm_control_action", "storm_control_trap",
        ):
            assert key in port


def test_list_ports_no_rows_yet_returns_empty(client):
    """A device with nothing synced yet returns an empty, valid envelope
    (not an error) -- cache-first reads never touch the live device. Uses
    its own device name (not the shared `_DEVICE`/`seeded_ports` one) --
    the Postgres test DB is not reset between test functions."""
    dev = get_or_create_device("list_dev_empty", site_name="Ports List Test Site")
    res = client.get(_url(dev.name))
    assert res.status_code == 200
    payload = res.json()["data"]["data"]
    assert payload["count"] == 0
    assert payload["ports"] == []


# -- Error mapping ---------------------------------------------------------------

def test_list_ports_unknown_device_returns_403(client):
    """Confirmed current behavior (verified by reading app/api/ports.py):
    `list_ports()` calls `_authz_device()` BEFORE `require_device()` --
    unlike the write endpoints, which fetch the device first. For a
    device name that resolves to no site/group, `authorize_device()`
    treats it as `role=None` and raises 403 -- even for an admin/
    system-admin caller, since `VisibilityScope.rol_para()` is never
    reached when `resolved is None` (the system-admin bypass lives
    inside `rol_para()`, which this code path skips entirely). So an
    unknown device 404's on every WRITE endpoint but 403's here on this
    READ endpoint -- a real, confirmed inconsistency, not a mistake in
    this test."""
    res = client.get(_url("__definitely_not_a_device__"))
    assert res.status_code == 403


# -- RBAC -------------------------------------------------------------------------

def test_list_ports_observer_can_read(observer_client, seeded_ports):
    res = observer_client.get(_url())
    assert res.status_code == 200


def test_list_ports_operator_can_read(operator_client, seeded_ports):
    res = operator_client.get(_url())
    assert res.status_code == 200


def test_list_ports_unauthenticated_rejected(unauth_client, seeded_ports):
    res = unauth_client.get(_url())
    assert res.status_code == 401


# -- Response model conformance ---------------------------------------------------

def test_response_model_includes_unknown_fields_as_null(client, seeded_ports):
    res = client.get(_url())
    payload = res.json()["data"]["data"]
    # The access port above never sets storm-control -- must surface as
    # null in the wire format, never as a missing key.
    port_without_storm = next(p for p in payload["ports"] if p["mode"] == "access")
    assert port_without_storm["storm_control_enabled"] is None
    assert port_without_storm["storm_control_threshold"] is None


def test_response_includes_sync_metadata(client, seeded_ports):
    res = client.get(_url())
    data = res.json()["data"]
    assert "synced_at" in data
    assert "sync_error" in data
    assert "sync_in_progress" in data
