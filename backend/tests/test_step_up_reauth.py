"""Tests for step-up re-authentication (PR #5 of the session-hardening plan).

Covers:
- ``POST /auth/reauth``: success returns an elevated token whose ``typ``
  discriminator is ``elevated``; wrong password → 401.
- ``require_elevated`` dependency: rejects missing / expired / wrong-user
  tokens, and rejects a plain access token used in the elevated header.
- Sensitive endpoints reject without the header (401 detail="reauth_required")
  and succeed with a valid elevated header.
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import (
    ELEVATED_TOKEN_EXPIRE_MINUTES,
    JWT_SECRET_KEY,
)
from app.core.security import (
    ALGORITHM,
    TOKEN_TYPE_ELEVATED,
    create_access_token,
    create_elevated_token,
    verify_elevated_token,
)


# ── Unit: elevated token roundtrip ───────────────────────────────────────────

def test_create_elevated_token_encodes_typ_and_sub():
    token = create_elevated_token("alice")
    payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
    assert payload["typ"] == TOKEN_TYPE_ELEVATED
    assert payload["sub"] == "alice"


def test_verify_elevated_token_accepts_matching_username():
    token = create_elevated_token("alice")
    verify_elevated_token(token, "alice")  # no exception


def test_verify_elevated_token_rejects_username_mismatch():
    token = create_elevated_token("alice")
    with pytest.raises(ValueError, match="elevated_mismatch"):
        verify_elevated_token(token, "bob")


def test_verify_elevated_token_rejects_expired():
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "alice",
        "typ": TOKEN_TYPE_ELEVATED,
        "iat": now - timedelta(minutes=10),
        "exp": now - timedelta(seconds=1),
    }
    stale = jwt.encode(payload, JWT_SECRET_KEY, algorithm=ALGORITHM)
    with pytest.raises(ValueError, match="elevated_expired"):
        verify_elevated_token(stale, "alice")


def test_verify_elevated_token_rejects_access_token():
    """An access token must not be usable as an elevated token."""
    access = create_access_token({"sub": "alice", "is_system_admin": True})
    with pytest.raises(ValueError, match="elevated_invalid"):
        verify_elevated_token(access, "alice")


# ── Integration: /auth/reauth endpoint ──────────────────────────────────────

def _login(client, username="admin", password="admin123"):
    r = client.post("/api/v1/auth/login", data={"username": username, "password": password})
    assert r.status_code == 200
    return r.json()["access_token"]


def test_reauth_returns_elevated_token(unauth_client):
    access = _login(unauth_client)
    r = unauth_client.post(
        "/api/v1/auth/reauth",
        json={"password": "admin123"},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "elevated_token" in data
    assert data["expires_in"] == ELEVATED_TOKEN_EXPIRE_MINUTES * 60


def test_reauth_rejects_wrong_password(unauth_client):
    access = _login(unauth_client)
    r = unauth_client.post(
        "/api/v1/auth/reauth",
        json={"password": "wrong"},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code == 401


def test_reauth_requires_authentication(unauth_client):
    r = unauth_client.post("/api/v1/auth/reauth", json={"password": "admin123"})
    assert r.status_code == 401


# ── Integration: sensitive endpoints require the header ──────────────────────

def _reauth(client, access, password):
    r = client.post(
        "/api/v1/auth/reauth",
        json={"password": password},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["elevated_token"]


def test_delete_site_requires_reauth_header(admin_client):
    """Without X-Elevated-Auth the endpoint 401s with reauth_required."""
    # Create a disposable site to attempt the delete on.
    created = admin_client.post(
        "/api/v1/sites/", json={"name": "step-up-test-site", "kind": "REGULAR"}
    )
    assert created.status_code in (200, 201), created.text
    site_id = created.json()["data"]["id"]

    r = admin_client.delete(f"/api/v1/sites/{site_id}")
    assert r.status_code == 401
    payload = r.json().get("detail") or r.json().get("message")
    assert "reauth_required" in str(payload)


def test_delete_site_accepts_valid_elevated_token(unauth_client):
    """Full happy path: login → reauth → delete with header succeeds."""
    access = _login(unauth_client)
    # Create a disposable site.
    r = unauth_client.post(
        "/api/v1/sites/",
        json={"name": "step-up-happy-site", "kind": "REGULAR"},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code in (200, 201), r.text
    site_id = r.json()["data"]["id"]

    elevated = _reauth(unauth_client, access, "admin123")
    r = unauth_client.delete(
        f"/api/v1/sites/{site_id}",
        headers={
            "Authorization": f"Bearer {access}",
            "X-Elevated-Auth": elevated,
        },
    )
    assert r.status_code == 200, r.text


def test_delete_site_rejects_access_token_as_elevated(unauth_client):
    """An attacker who steals a bearer must not be able to reuse it as the
    elevated header — enforced by the ``typ`` check in verify_elevated_token."""
    access = _login(unauth_client)
    r = unauth_client.post(
        "/api/v1/sites/",
        json={"name": "step-up-typ-check", "kind": "REGULAR"},
        headers={"Authorization": f"Bearer {access}"},
    )
    assert r.status_code in (200, 201)
    site_id = r.json()["data"]["id"]

    r = unauth_client.delete(
        f"/api/v1/sites/{site_id}",
        headers={
            "Authorization": f"Bearer {access}",
            "X-Elevated-Auth": access,  # same bearer, wrong ``typ``
        },
    )
    assert r.status_code == 401
    payload = r.json().get("detail") or r.json().get("message")
    assert "reauth_required" in str(payload)


def test_delete_site_rejects_elevated_token_for_other_user(unauth_client):
    """Alice's elevated token must not authorise Bob's requests."""
    admin_access = _login(unauth_client)
    other = create_elevated_token("some-other-user")

    # Create a disposable site as admin.
    r = unauth_client.post(
        "/api/v1/sites/",
        json={"name": "step-up-other-user", "kind": "REGULAR"},
        headers={"Authorization": f"Bearer {admin_access}"},
    )
    assert r.status_code in (200, 201)
    site_id = r.json()["data"]["id"]

    r = unauth_client.delete(
        f"/api/v1/sites/{site_id}",
        headers={
            "Authorization": f"Bearer {admin_access}",
            "X-Elevated-Auth": other,
        },
    )
    assert r.status_code == 401
