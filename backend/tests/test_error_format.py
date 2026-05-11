"""Tests for SEC-004 — Standardized error response format."""
import pytest

from app.schemas.user import UserCreate
from app.services import user_service


_REQUIRED_KEYS = {"error_code", "message", "details", "timestamp"}


def _assert_error_shape(response, expected_status: int, expected_code: str | None = None):
    assert response.status_code == expected_status
    body = response.json()
    assert _REQUIRED_KEYS == _REQUIRED_KEYS & body.keys(), f"Missing keys in: {body}"
    assert isinstance(body["error_code"], str) and body["error_code"]
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["timestamp"], str) and "T" in body["timestamp"]
    if expected_code:
        assert body["error_code"] == expected_code
    return body


# ── 401 Unauthorized ──────────────────────────────────────────────────────────

def test_401_no_token_has_standard_format(unauth_client):
    r = unauth_client.get("/api/v1/vlans/")
    _assert_error_shape(r, 401)


def test_401_invalid_token_has_standard_format(unauth_client):
    r = unauth_client.get("/api/v1/vlans/", headers={"Authorization": "Bearer bad.token"})
    _assert_error_shape(r, 401)


def test_401_invalid_credentials_has_standard_format(unauth_client):
    r = unauth_client.post("/api/v1/auth/login", data={"username": "nobody", "password": "x"})
    _assert_error_shape(r, 401, "UNAUTHORIZED")


# ── 403 Forbidden ─────────────────────────────────────────────────────────────

def test_403_insufficient_role_has_standard_format(observer_client):
    r = observer_client.post("/api/v1/vlans/", json={"vlan_id": 10, "name": "X", "devices": ["mock_device"]})
    _assert_error_shape(r, 403, "FORBIDDEN")


# ── 404 Not Found ─────────────────────────────────────────────────────────────

def test_404_unknown_device_has_standard_format(client):
    r = client.get("/api/v1/devices/nonexistent_device_xyz")
    _assert_error_shape(r, 404, "NOT_FOUND")


def test_404_unknown_job_has_standard_format(client):
    r = client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000")
    _assert_error_shape(r, 404, "NOT_FOUND")


# ── 422 Validation Error ──────────────────────────────────────────────────────

def test_422_pydantic_field_error_has_standard_format(client):
    r = client.post("/api/v1/vlans/", json={"vlan_id": 9999, "name": "X", "devices": ["mock_device"]})
    body = _assert_error_shape(r, 422, "VALIDATION_ERROR")
    assert "errors" in body["details"]
    assert isinstance(body["details"]["errors"], list)


def test_422_contains_field_error_type(client):
    r = client.post("/api/v1/vlans/", json={"vlan_id": 9999, "name": "X", "devices": ["mock_device"]})
    assert r.status_code == 422
    errors = r.json()["details"]["errors"]
    assert any(e.get("type") == "less_than_equal" for e in errors)


def test_400_app_validation_error_has_standard_format(client, monkeypatch):
    from app.services import vlan_service
    monkeypatch.setattr(vlan_service, "EXECUTION_MODE", "real")
    r = client.get("/api/v1/vlans/")
    body = _assert_error_shape(r, 400, "VALIDATION_ERROR")
    assert "device" in body["message"].lower()


# ── 429 Rate Limit ────────────────────────────────────────────────────────────

def test_429_rate_limit_has_standard_format(unauth_client, monkeypatch):
    from app.core import rate_limit_middleware as rl
    monkeypatch.setattr(rl, "RATE_LIMIT_PER_IP_RPM", 1)
    rl.reset()
    unauth_client.get("/")
    r = unauth_client.get("/")
    body = _assert_error_shape(r, 429, "RATE_LIMIT_EXCEEDED")
    assert "retry-after" in r.headers


# ── 500 Internal Error ────────────────────────────────────────────────────────

def test_500_does_not_leak_stack_trace(monkeypatch):
    from fastapi.testclient import TestClient
    from app.core.security import create_access_token
    from app.main import app
    from app.services import device_service

    def _boom():
        raise RuntimeError("internal detail that must not leak")

    monkeypatch.setattr(device_service, "get_devices", _boom)

    # raise_server_exceptions=False lets the 500 handler respond instead of re-raising
    safe_client = TestClient(app, raise_server_exceptions=False)
    token = create_access_token({"sub": "admin", "role": "admin"})
    safe_client.headers.update({"Authorization": f"Bearer {token}"})

    r = safe_client.get("/api/v1/devices/")
    body = _assert_error_shape(r, 500, "INTERNAL_ERROR")
    assert "internal detail" not in str(body)
    assert "Traceback" not in str(body)


# ── Timestamp format ──────────────────────────────────────────────────────────

def test_timestamp_is_iso8601_utc(unauth_client):
    r = unauth_client.get("/api/v1/vlans/")
    ts = r.json()["timestamp"]
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(ts)
    assert dt.tzinfo is not None
