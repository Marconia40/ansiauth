"""Tests for OPS-001 — Health check endpoint."""
from datetime import datetime, timezone

import pytest


# ── Basic contract ────────────────────────────────────────────────────────────

def test_health_returns_200_when_db_healthy(unauth_client):
    r = unauth_client.get("/health")
    assert r.status_code == 200


def test_health_response_contains_required_fields(unauth_client):
    r = unauth_client.get("/health")
    body = r.json()
    assert "status" in body
    assert "db_status" in body
    assert "timestamp" in body
    assert "version" in body


def test_health_status_ok_when_healthy(unauth_client):
    r = unauth_client.get("/health")
    assert r.json()["status"] == "ok"
    assert r.json()["db_status"] == "ok"


def test_health_timestamp_is_iso8601_utc(unauth_client):
    r = unauth_client.get("/health")
    ts = r.json()["timestamp"]
    dt = datetime.fromisoformat(ts)
    assert dt.tzinfo is not None


def test_health_version_is_string(unauth_client):
    r = unauth_client.get("/health")
    assert isinstance(r.json()["version"], str)


# ── No authentication required ────────────────────────────────────────────────

def test_health_no_auth_required(unauth_client):
    r = unauth_client.get("/health")
    assert r.status_code == 200


def test_health_accessible_without_token(unauth_client):
    r = unauth_client.get("/health", headers={})
    assert r.status_code != 401


# ── DB-down scenario ──────────────────────────────────────────────────────────

def test_health_returns_503_when_db_unreachable(unauth_client, monkeypatch):
    from app.api import health as health_mod
    monkeypatch.setattr(health_mod, "_ping_db", lambda: False)
    r = unauth_client.get("/health")
    assert r.status_code == 503


def test_health_status_down_when_db_unreachable(unauth_client, monkeypatch):
    from app.api import health as health_mod
    monkeypatch.setattr(health_mod, "_ping_db", lambda: False)
    r = unauth_client.get("/health")
    body = r.json()
    assert body["status"] == "down"
    assert body["db_status"] == "down"


def test_health_503_still_returns_json(unauth_client, monkeypatch):
    from app.api import health as health_mod
    monkeypatch.setattr(health_mod, "_ping_db", lambda: False)
    r = unauth_client.get("/health")
    body = r.json()
    assert "status" in body and "timestamp" in body


# ── Rate limiter exclusion ────────────────────────────────────────────────────

def test_health_not_rate_limited(unauth_client, monkeypatch):
    """Health endpoint is excluded from the per-IP rate limiter."""
    from app.core import rate_limit_middleware as rl
    monkeypatch.setattr(rl, "RATE_LIMIT_PER_IP_RPM", 1)
    rl.reset()

    # First request consumes the only slot for normal IPs
    unauth_client.get("/")

    # Health check must still be accessible even with rate limit exhausted
    r = unauth_client.get("/health")
    assert r.status_code == 200
