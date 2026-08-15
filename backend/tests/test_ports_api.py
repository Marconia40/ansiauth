"""API-level tests for ``GET /api/v1/ports/`` (Step 1.2).

The driver-side parser and service layer are covered by ``test_port_parser``
and ``test_port_service``.  Here we focus on the HTTP contract: response
envelope shape, role enforcement, error mapping, and site-scoped RBAC.
"""

from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.main import app
from app.services import port_service


def _client_with_role(role: str) -> TestClient:
    token = create_access_token({"sub": role, "role": role})
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


# ── Happy path (mock mode) ───────────────────────────────────────────────────

def test_list_ports_returns_envelope(monkeypatch, client):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")
    res = client.get("/api/v1/ports/?device=mock_device")
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    data = body["data"]
    # Envelope shape
    assert data["device"] == "mock_device"
    assert data["vendor"] == "mock"
    assert data["count"] == len(data["ports"])
    assert isinstance(data["ports"], list)
    assert len(data["ports"]) > 0
    # Every documented field is present per row
    for port in data["ports"]:
        for key in (
            "name", "description", "admin_up", "operational_up", "mode",
            "access_vlan", "allowed_vlans", "poe_enabled", "speed", "duplex",
        ):
            assert key in port


def test_list_ports_without_device_in_mock_mode_uses_default(monkeypatch, client):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")
    res = client.get("/api/v1/ports/")
    assert res.status_code == 200
    body = res.json()["data"]
    # In mock mode the endpoint substitutes the canonical mock device name.
    assert body["device"] == "mock_device"
    assert body["count"] > 0


def test_list_ports_requires_device_in_real_mode(monkeypatch, client):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "real")
    res = client.get("/api/v1/ports/")
    assert res.status_code == 400
    body = res.json()
    # Standard validation error envelope
    assert body["message"] == "'device' query parameter is required"


# ── Error mapping ────────────────────────────────────────────────────────────

def test_list_ports_unknown_device_returns_404(monkeypatch, client):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "real")
    # In real mode the service resolves devices via device_service.get_device
    # and raises ValueError for unknown ids → mapped to 404.
    res = client.get("/api/v1/ports/?device=__definitely_not_a_device__")
    assert res.status_code == 404


def test_list_ports_propagates_device_failure(monkeypatch, client):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")
    # ``fail_device`` is wired in port_service mock mode to raise RuntimeError.
    res = client.get("/api/v1/ports/?device=fail_device")
    assert res.status_code == 500


# ── RBAC ─────────────────────────────────────────────────────────────────────

def test_list_ports_observer_can_read(monkeypatch):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")
    observer = _client_with_role("observer")
    res = observer.get("/api/v1/ports/?device=mock_device")
    assert res.status_code == 200


def test_list_ports_operator_can_read(monkeypatch):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")
    operator = _client_with_role("operator")
    res = operator.get("/api/v1/ports/?device=mock_device")
    assert res.status_code == 200


def test_list_ports_unauthenticated_rejected(monkeypatch):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")
    anon = TestClient(app)
    res = anon.get("/api/v1/ports/?device=mock_device")
    assert res.status_code == 401


# ── Response model conformance ───────────────────────────────────────────────

def test_response_model_includes_unknown_fields_as_null(monkeypatch, client):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")
    res = client.get("/api/v1/ports/?device=mock_device")
    data = res.json()["data"]
    # Mock data deliberately leaves PoE / speed / duplex unknown — they must
    # surface as null in the wire format, never as missing keys.
    port_without_extras = next(p for p in data["ports"] if p["mode"] == "access")
    assert port_without_extras["poe_enabled"] is None
    assert port_without_extras["speed"] is None
    assert port_without_extras["duplex"] is None
