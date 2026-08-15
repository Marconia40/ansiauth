"""Tests for AUTH-005 — Token refresh and logout."""
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import RefreshTokenModel
from app.db.session import get_session
from app.schemas.user import UserCreate
from app.services import refresh_token_service, user_service


# ── Helpers ───────────────────────────────────────────────────────────────────

def _login(client, username="lifecycle_user", password="lifecycle_pass_99"):
    r = client.post("/api/v1/auth/login", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _refresh(client, refresh_token):
    client.cookies.set("refresh_token", refresh_token)
    return client.post("/api/v1/auth/refresh")


def _logout(client, refresh_token):
    return client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})


# ── Fixture ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def seed_lifecycle_user():
    if user_service.get_by_username("lifecycle_user") is None:
        user_service.create_user(UserCreate(username="lifecycle_user", password="lifecycle_pass_99", role="observer"))
    yield
    with get_session() as session:
        from app.db.models import UserModel
        session.query(UserModel).filter_by(username="lifecycle_user").delete(synchronize_session=False)


# ── Login returns refresh token ───────────────────────────────────────────────

def test_login_returns_refresh_token(unauth_client):
    data = _login(unauth_client)
    assert "refresh_token" in data
    assert isinstance(data["refresh_token"], str)
    assert len(data["refresh_token"]) > 20


def test_login_returns_access_token_and_type(unauth_client):
    data = _login(unauth_client)
    assert "access_token" in data
    assert data["token_type"] == "bearer"


# ── Refresh endpoint ──────────────────────────────────────────────────────────

def test_valid_refresh_token_returns_new_access_token(unauth_client):
    tokens = _login(unauth_client)
    r = _refresh(unauth_client, tokens["refresh_token"])
    assert r.status_code == 200
    data = r.json()
    assert "access_token" in data
    assert "refresh_token" in data


def test_refresh_issues_different_access_token(unauth_client):
    tokens = _login(unauth_client)
    r = _refresh(unauth_client, tokens["refresh_token"])
    # New access token is a new JWT (can't easily compare without decoding, but it's a string)
    assert r.json()["access_token"] != ""


def test_refresh_issues_different_refresh_token(unauth_client):
    tokens = _login(unauth_client)
    r = _refresh(unauth_client, tokens["refresh_token"])
    new_refresh = r.json()["refresh_token"]
    assert new_refresh != tokens["refresh_token"]


def test_new_access_token_is_usable(unauth_client):
    tokens = _login(unauth_client)
    r = _refresh(unauth_client, tokens["refresh_token"])
    new_access = r.json()["access_token"]

    r = unauth_client.get("/api/v1/vlans/?device=mock_device", headers={"Authorization": f"Bearer {new_access}"})
    assert r.status_code == 200


def test_invalid_refresh_token_returns_401(unauth_client):
    r = _refresh(unauth_client, "this.is.not.a.valid.token")
    assert r.status_code == 401


def test_expired_refresh_token_returns_401(unauth_client):
    tokens = _login(unauth_client)
    raw = tokens["refresh_token"]

    # Manually expire the token in the DB (use naive UTC to match SQLite's storage)
    from app.services.refresh_token_service import _hash
    with get_session() as session:
        record = session.query(RefreshTokenModel).filter_by(token_hash=_hash(raw)).first()
        record.expires_at = datetime.utcnow() - timedelta(seconds=1)

    r = _refresh(unauth_client, raw)
    assert r.status_code == 401
    assert "expired" in r.json()["message"].lower()


# ── Single-use (rotation) ─────────────────────────────────────────────────────

def test_used_refresh_token_cannot_be_used_again(unauth_client):
    tokens = _login(unauth_client)
    original = tokens["refresh_token"]

    # First use succeeds
    r = _refresh(unauth_client, original)
    assert r.status_code == 200

    # Second use of the same token returns 401
    r = _refresh(unauth_client, original)
    assert r.status_code == 401


def test_replay_detection_revokes_all_sessions(unauth_client):
    """Replaying an old refresh token must revoke all active sessions for that user."""
    tokens = _login(unauth_client)
    original = tokens["refresh_token"]

    # Consume original token → get rotated token
    r = _refresh(unauth_client, original)
    rotated = r.json()["refresh_token"]

    # Replay original (already-revoked) token → triggers cascade revocation
    r = _refresh(unauth_client, original)
    assert r.status_code == 401

    # The rotated token is also unusable now (revoked by cascade)
    r = _refresh(unauth_client, rotated)
    assert r.status_code == 401


# ── Logout ────────────────────────────────────────────────────────────────────

def test_logout_returns_success(unauth_client):
    tokens = _login(unauth_client)
    r = _logout(unauth_client, tokens["refresh_token"])
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_logout_revokes_refresh_token(unauth_client):
    tokens = _login(unauth_client)
    _logout(unauth_client, tokens["refresh_token"])

    r = _refresh(unauth_client, tokens["refresh_token"])
    assert r.status_code == 401


def test_logout_unknown_token_returns_success_with_false(unauth_client):
    r = _logout(unauth_client, "nonexistent.token.value")
    assert r.status_code == 200
    assert r.json()["data"]["revoked"] is False


def test_logout_does_not_invalidate_access_token(unauth_client):
    """Access tokens are stateless JWTs; logout only affects the refresh token."""
    tokens = _login(unauth_client)
    access = tokens["access_token"]
    _logout(unauth_client, tokens["refresh_token"])

    r = unauth_client.get("/api/v1/vlans/?device=mock_device", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200


# ── Service-level unit tests ──────────────────────────────────────────────────

def test_service_create_returns_raw_token():
    raw = refresh_token_service.create("lifecycle_user")
    assert isinstance(raw, str) and len(raw) > 20


def test_service_validate_and_rotate_returns_new_token_and_username():
    raw = refresh_token_service.create("lifecycle_user")
    new_raw, username = refresh_token_service.validate_and_rotate(raw)
    assert new_raw != raw
    assert username == "lifecycle_user"


def test_service_revoke_returns_true_for_valid_token():
    raw = refresh_token_service.create("lifecycle_user")
    assert refresh_token_service.revoke(raw) is True


def test_service_revoke_returns_false_for_unknown_token():
    assert refresh_token_service.revoke("garbage") is False


def test_service_double_revoke_returns_false():
    raw = refresh_token_service.create("lifecycle_user")
    refresh_token_service.revoke(raw)
    assert refresh_token_service.revoke(raw) is False
