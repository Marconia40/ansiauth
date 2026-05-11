"""Tests for SEC-003 — Per-IP and per-user rate limiting."""
import pytest

from app.core import rate_limit_middleware as rl


@pytest.fixture(autouse=True)
def real_limits():
    """Restore real rate limits for this test file, then clean up after each test."""
    prev_ip = rl.RATE_LIMIT_PER_IP_RPM
    prev_user = rl.RATE_LIMIT_PER_USER_RPM
    prev_login = rl.LOGIN_RATE_LIMIT_RPM
    rl.RATE_LIMIT_PER_IP_RPM = 20
    rl.RATE_LIMIT_PER_USER_RPM = 200
    rl.LOGIN_RATE_LIMIT_RPM = 5
    rl.reset()
    yield
    rl.RATE_LIMIT_PER_IP_RPM = prev_ip
    rl.RATE_LIMIT_PER_USER_RPM = prev_user
    rl.LOGIN_RATE_LIMIT_RPM = prev_login
    rl.reset()


# ── Unauthenticated (per-IP) ──────────────────────────────────────────────────

def test_20_unauthenticated_requests_succeed(unauth_client):
    for _ in range(20):
        r = unauth_client.get("/")
        assert r.status_code == 200


def test_21st_unauthenticated_request_returns_429(unauth_client):
    for _ in range(20):
        unauth_client.get("/")

    r = unauth_client.get("/")
    assert r.status_code == 429


def test_retry_after_header_present_on_429(unauth_client):
    for _ in range(20):
        unauth_client.get("/")

    r = unauth_client.get("/")
    assert r.status_code == 429
    assert "retry-after" in r.headers
    retry_after = int(r.headers["retry-after"])
    assert 0 < retry_after <= 60


def test_rate_limit_resets_after_window(unauth_client, monkeypatch):
    for _ in range(20):
        unauth_client.get("/")

    r = unauth_client.get("/")
    assert r.status_code == 429

    # Advance time 61 seconds past the window
    real_now = rl._get_now()
    monkeypatch.setattr(rl, "_get_now", lambda: real_now + 61)

    r = unauth_client.get("/")
    assert r.status_code == 200


# ── Login endpoint (stricter: 5/min) ─────────────────────────────────────────

def test_login_limit_is_stricter_than_general_ip_limit(unauth_client):
    for _ in range(5):
        unauth_client.post("/api/v1/auth/login", data={"username": "x", "password": "y"})

    r = unauth_client.post("/api/v1/auth/login", data={"username": "x", "password": "y"})
    assert r.status_code == 429


def test_login_rate_limit_is_independent_of_general_ip_bucket(unauth_client):
    # Exhaust the login-specific bucket (5/min)
    for _ in range(5):
        unauth_client.post("/api/v1/auth/login", data={"username": "x", "password": "y"})

    # Other unauthenticated endpoints use a separate bucket — still accessible
    r = unauth_client.get("/")
    assert r.status_code == 200


# ── Authenticated (per-user) ──────────────────────────────────────────────────

def test_authenticated_requests_use_user_limit(client, monkeypatch):
    # Override user limit to a small value for fast testing
    monkeypatch.setattr(rl, "RATE_LIMIT_PER_USER_RPM", 5)

    for _ in range(5):
        r = client.get("/api/v1/vlans/")
        assert r.status_code == 200

    r = client.get("/api/v1/vlans/")
    assert r.status_code == 429


def test_authenticated_limit_is_separate_from_ip_limit(client, unauth_client, monkeypatch):
    """An authenticated user near their limit does not affect the IP bucket for unauthenticated users."""
    monkeypatch.setattr(rl, "RATE_LIMIT_PER_USER_RPM", 3)

    for _ in range(3):
        client.get("/api/v1/vlans/")

    # Authenticated user is now blocked
    assert client.get("/api/v1/vlans/").status_code == 429

    # Unauthenticated IP bucket is untouched
    assert unauth_client.get("/").status_code == 200


# ── Limits configurable via env vars ─────────────────────────────────────────

def test_per_ip_limit_uses_module_variable(unauth_client, monkeypatch):
    monkeypatch.setattr(rl, "RATE_LIMIT_PER_IP_RPM", 3)
    rl.reset()

    for _ in range(3):
        unauth_client.get("/")

    r = unauth_client.get("/")
    assert r.status_code == 429


def test_login_limit_uses_module_variable(unauth_client, monkeypatch):
    monkeypatch.setattr(rl, "LOGIN_RATE_LIMIT_RPM", 2)
    rl.reset()

    for _ in range(2):
        unauth_client.post("/api/v1/auth/login", data={"username": "x", "password": "y"})

    r = unauth_client.post("/api/v1/auth/login", data={"username": "x", "password": "y"})
    assert r.status_code == 429
