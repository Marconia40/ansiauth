"""End-to-end multi-vendor validation for Step 1.4.

These tests assert that the *exact same* request flow (port_service →
dispatcher → driver → parser → PortInfo) produces an identical envelope
shape for Huawei and Cisco, and that an unknown vendor surfaces the
operator-friendly 501 response instead of a raw backend error.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import UnsupportedVendorError
from app.core.security import create_access_token
from app.main import app
from app.models.device import Device
from app.models.port import PortInfo
from app.services import ansible_service, port_service
from app.services.vendors.dispatcher import get_port_driver


def _admin_client() -> TestClient:
    token = create_access_token({"sub": "admin", "role": "admin"})
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


# Minimal-but-realistic stdouts so each driver actually returns a populated
# inventory, exercising the full parser stack of each vendor.
_HUAWEI_BRIEF = """\
Interface                   PHY     Protocol  InUti OutUti   inErrors  outErrors
GigabitEthernet0/0/1        up      up        0%    0%             0        0
"""
_HUAWEI_DESC = """\
Interface                  PHY      Protocol Description
GigabitEthernet0/0/1       up       up       cross-vendor-test
"""
_HUAWEI_PORTVLAN = """\
Port                    Link Type    PVID  Trunk VLAN List
-------------------------------------------------------------------
GigabitEthernet0/0/1    access       10    -
"""

_CISCO_STATUS = """\
Port      Name               Status       Vlan       Duplex  Speed Type
Gi0/1     cross-vendor-test  connected    10         a-full  a-1000 10/100/1000BaseTX
"""
_CISCO_DESC = """\
Interface                      Status         Protocol Description
Gi0/1                          up             up       cross-vendor-test
"""
_CISCO_SW = """\
Name: Gi0/1
Switchport: Enabled
Administrative Mode: static access
Operational Mode: static access
Access Mode VLAN: 10 (TEST)
Trunking Native Mode VLAN: 1 (default)
Trunking VLANs Enabled: ALL
"""


# ── Service-level cross-vendor parity ─────────────────────────────────────────

def test_service_layer_returns_same_shape_for_both_vendors(monkeypatch):
    """``port_service.list_ports`` returns the same PortListResponse shape
    regardless of whether the underlying driver is Huawei or Cisco —
    callers should never need vendor branching."""

    huawei_device = Device(
        name="huawei-x",
        host="192.0.2.1",
        vendor="huawei_vrp",
        username="admin",
        encrypted_password="encrypted",
        platform="vrp",
    )
    cisco_device = Device(
        name="cisco-x",
        host="192.0.2.2",
        vendor="cisco_ios",
        username="admin",
        encrypted_password="encrypted",
        platform="ios",
    )

    def _fake_get_device(device_id):
        return {"huawei-x": huawei_device, "cisco-x": cisco_device}.get(device_id)

    monkeypatch.setattr("app.services.device_service.get_device", _fake_get_device)
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "real")
    monkeypatch.setattr(
        "app.services.secret_service.decrypt_password",
        lambda enc: "fake-password",
    )

    huawei_stdouts = [_HUAWEI_BRIEF, _HUAWEI_DESC, _HUAWEI_PORTVLAN]
    cisco_stdouts = [_CISCO_STATUS, _CISCO_DESC, _CISCO_SW]

    def _fake_run_playbook(playbook, extravars, inventory=None, device=None):
        if "huawei" in playbook:
            return {"rc": 0, "stdout": "", "stderr": "", "stdouts": huawei_stdouts}
        if "cisco" in playbook:
            return {"rc": 0, "stdout": "", "stderr": "", "stdouts": cisco_stdouts}
        raise AssertionError(f"unexpected playbook: {playbook}")

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run_playbook)

    h = port_service.list_ports("huawei-x")
    c = port_service.list_ports("cisco-x")

    # Envelope structure parity
    assert set(h.to_dict().keys()) == set(c.to_dict().keys())
    assert h.device == "huawei-x"
    assert c.device == "cisco-x"
    assert h.vendor == "huawei_vrp"
    assert c.vendor == "cisco_ios"
    assert len(h.ports) == 1
    assert len(c.ports) == 1

    # Per-port field parity
    hp, cp = h.ports[0], c.ports[0]
    assert isinstance(hp, PortInfo)
    assert isinstance(cp, PortInfo)
    assert set(hp.to_dict().keys()) == set(cp.to_dict().keys())

    # Both descriptions populated identically — proves the normalized model
    # surfaces the same operator-facing info regardless of vendor CLI dialect.
    assert hp.description == "cross-vendor-test"
    assert cp.description == "cross-vendor-test"
    assert hp.mode == "access"
    assert cp.mode == "access"
    assert hp.access_vlan == 10
    assert cp.access_vlan == 10

    # PoE / speed / duplex must be None across both vendors per the Step 1.x spec
    for p in (hp, cp):
        assert p.poe_enabled is None
        assert p.speed is None
        assert p.duplex is None


# ── API-level cross-vendor parity ─────────────────────────────────────────────

def test_api_envelope_identical_for_both_vendors(monkeypatch):
    """``GET /api/v1/ports/?device=...`` returns the same envelope keys
    regardless of vendor — the frontend can use a single rendering path."""

    huawei_device = Device(
        name="huawei-api",
        host="192.0.2.10",
        vendor="huawei_vrp",
        username="admin",
        encrypted_password="encrypted",
        platform="vrp",
    )
    cisco_device = Device(
        name="cisco-api",
        host="192.0.2.11",
        vendor="cisco_ios",
        username="admin",
        encrypted_password="encrypted",
        platform="ios",
    )

    def _fake_get_device(device_id):
        return {"huawei-api": huawei_device, "cisco-api": cisco_device}.get(device_id)

    monkeypatch.setattr("app.services.device_service.get_device", _fake_get_device)
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "real")
    monkeypatch.setattr(
        "app.services.secret_service.decrypt_password",
        lambda enc: "fake-password",
    )

    def _fake_run_playbook(playbook, extravars, inventory=None, device=None):
        if "huawei" in playbook:
            return {
                "rc": 0, "stdout": "", "stderr": "",
                "stdouts": [_HUAWEI_BRIEF, _HUAWEI_DESC, _HUAWEI_PORTVLAN],
            }
        if "cisco" in playbook:
            return {
                "rc": 0, "stdout": "", "stderr": "",
                "stdouts": [_CISCO_STATUS, _CISCO_DESC, _CISCO_SW],
            }
        raise AssertionError(f"unexpected playbook: {playbook}")

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run_playbook)

    client = _admin_client()
    huawei_resp = client.get("/api/v1/ports/?device=huawei-api")
    cisco_resp = client.get("/api/v1/ports/?device=cisco-api")

    assert huawei_resp.status_code == 200
    assert cisco_resp.status_code == 200

    huawei_body = huawei_resp.json()
    cisco_body = cisco_resp.json()
    assert set(huawei_body.keys()) == set(cisco_body.keys())
    assert huawei_body["success"] is True
    assert cisco_body["success"] is True

    huawei_data = huawei_body["data"]
    cisco_data = cisco_body["data"]
    assert set(huawei_data.keys()) == set(cisco_data.keys())

    # Per-row schema parity (same key set, same field presence)
    assert set(huawei_data["ports"][0].keys()) == set(cisco_data["ports"][0].keys())


# ── Unsupported-vendor UX ─────────────────────────────────────────────────────

def test_dispatcher_carries_vendor_and_platform_on_unsupported():
    fake_device = Device(
        name="juniper-x",
        host="192.0.2.20",
        vendor="juniper",
        username="admin",
        encrypted_password="encrypted",
        platform="junos",
    )
    with pytest.raises(UnsupportedVendorError) as exc:
        get_port_driver(fake_device)
    assert exc.value.vendor == "juniper"
    assert exc.value.platform == "junos"


def test_api_returns_501_friendly_message_for_unsupported_vendor(monkeypatch):
    """The raw 'No port driver registered...' string MUST NOT reach the client.

    The dispatcher's WARNING log keeps the diagnostic detail; the HTTP body
    only carries the operator-friendly notice and a stable error_code.
    """
    fake_device = Device(
        name="juniper-x",
        host="192.0.2.20",
        vendor="juniper",
        username="admin",
        encrypted_password="encrypted",
        platform="junos",
    )

    monkeypatch.setattr(port_service, "EXECUTION_MODE", "real")
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: fake_device if name == "juniper-x" else None,
    )
    monkeypatch.setattr(
        "app.services.secret_service.decrypt_password",
        lambda enc: "fake-password",
    )

    res = _admin_client().get("/api/v1/ports/?device=juniper-x")
    assert res.status_code == 501

    body = res.json()
    assert body["error_code"] == "VENDOR_NOT_SUPPORTED"
    assert body["message"] == "Port management is not yet supported for this vendor."

    # Defence: vendor and platform identifiers must NOT leak to the client body.
    blob = res.text.lower()
    assert "juniper" not in blob
    assert "junos" not in blob
    assert "no port driver registered" not in blob


def test_api_unsupported_vendor_message_is_stable_across_aliases(monkeypatch):
    """The same operator-friendly message is returned for any unsupported
    vendor / platform tuple — the message must not vary per input."""
    for vendor, platform in [("juniper", "junos"), ("arista", "eos"), ("foo", "bar")]:
        fake_device = Device(
            name=f"{vendor}-x",
            host="192.0.2.30",
            vendor=vendor,
            username="admin",
            encrypted_password="encrypted",
            platform=platform,
        )
        monkeypatch.setattr(port_service, "EXECUTION_MODE", "real")
        monkeypatch.setattr(
            "app.services.device_service.get_device",
            lambda name, d=fake_device: d if name == d.name else None,
        )
        monkeypatch.setattr(
            "app.services.secret_service.decrypt_password",
            lambda enc: "fake-password",
        )

        res = _admin_client().get(f"/api/v1/ports/?device={vendor}-x")
        assert res.status_code == 501
        assert res.json()["message"] == "Port management is not yet supported for this vendor."
