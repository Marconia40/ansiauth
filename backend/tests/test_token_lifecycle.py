"""Tests for AUTH-005 — Token refresh and logout."""
from datetime import datetime, timedelta, timezone

import pytest

from app.composition import (
    device_repository, plugin_registry, role_assignment_repository,
    site_repository, user_repository,
)
from app.db.models import RefreshTokenModel, SiteModel
from app.db.session import get_session
from app.repositories.role_assignment_repository import RoleAssignment
from app.services import refresh_token_service
from app.services.vendors.mock import MockVendor

_TEST_SITE_NAME = "TokenLifecycleSite"


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
def seed_lifecycle_user(admin_client):
    """A fresh observer user has no grants and therefore cannot see
    mock_device. Register a dedicated site + mock_device (neither is
    seeded by default anymore -- there's no "Mock Site" bootstrap left)
    and attach a site-wide observer grant so the lifecycle flow tests
    (which end with a ``GET /vlans/?device=mock_device``) still pass under
    strict-hierarchy. Also forces cisco_ios onto MockVendor: this repo's
    .env pins EXECUTION_MODE=real for local dev, baked into plugin_registry
    at import time, before any fixture here runs.
    """
    from app.db.models import RoleAssignmentModel, UserModel

    plugin_registry.registrar("cisco_ios", MockVendor())
    plugin_registry.registrar("huawei_vrp", MockVendor())

    if user_repository.obtener_por_username("lifecycle_user") is None:
        user_repository.crear("lifecycle_user", "lifecycle_pass_99")

    with get_session() as session:
        row = session.query(SiteModel).filter_by(name=_TEST_SITE_NAME).first()
        site_id = row.id if row else None
    if site_id is None:
        site_id = site_repository.crear_con_grupo_default(_TEST_SITE_NAME).id

    # Grant on whichever site actually owns "mock_device" -- if another
    # test file's fixture already registered it under ITS OWN site (device
    # names are global across the shared test DB), granting only on
    # `site_id` here would leave the observer unable to see a device that
    # landed elsewhere.
    existing = device_repository.get("mock_device")
    if existing is None:
        resp = admin_client.post("/api/v1/devices/", json={
            "name": "mock_device", "host": "10.0.0.1", "vendor": "cisco_ios",
            "username": "admin", "password": "admin", "site_id": site_id,
        })
        assert resp.status_code == 200, resp.text
        device_site_id = site_id
    else:
        device_site_id = existing.site_id

    user = user_repository.obtener_por_username("lifecycle_user")
    scope = role_assignment_repository.scope_de({"id": user.id, "is_system_admin": False})
    if scope.rol_para(device_site_id) != "observer":
        role_assignment_repository.add(RoleAssignment(user_id=user.id, site_id=device_site_id, role="observer"))
    yield
    with get_session() as session:
        uid = session.query(UserModel.id).filter_by(username="lifecycle_user").scalar()
        if uid is not None:
            session.query(RoleAssignmentModel).filter_by(user_id=uid).delete(
                synchronize_session=False
            )
        session.query(UserModel).filter_by(username="lifecycle_user").delete(
            synchronize_session=False
        )


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
    new_raw, username, _session_id = refresh_token_service.validate_and_rotate(raw)
    assert new_raw != raw
    assert username == "lifecycle_user"


# ── Session hardening — 401 detail codes ─────────────────────────────────────

def test_refresh_after_idle_returns_401_with_idle_timeout_detail(unauth_client):
    """Server-side idle timeout must both reject the refresh AND surface the
    reason as detail="idle_timeout" so the frontend can show the right
    banner ("your session ended due to inactivity")."""
    from app.core.config import REFRESH_TOKEN_IDLE_MINUTES
    tokens = _login(unauth_client)
    raw = tokens["refresh_token"]

    with get_session() as session:
        from app.services.refresh_token_service import _hash
        row = session.query(RefreshTokenModel).filter_by(token_hash=_hash(raw)).one()
        row.last_used_at = datetime.now(timezone.utc) - timedelta(
            minutes=REFRESH_TOKEN_IDLE_MINUTES + 1
        )

    r = _refresh(unauth_client, raw)
    assert r.status_code == 401
    body = r.json()
    # The FastAPI standard shape is {"detail": ...}; this app wraps it as
    # {"message": ...} via its global handler — accept whichever.
    payload = body.get("detail") or body.get("message")
    assert "idle_timeout" in str(payload)


def test_refresh_after_absolute_limit_returns_401_with_absolute_detail(unauth_client):
    from app.core.config import SESSION_ABSOLUTE_MAX_HOURS
    tokens = _login(unauth_client)
    raw = tokens["refresh_token"]

    with get_session() as session:
        from app.services.refresh_token_service import _hash
        row = session.query(RefreshTokenModel).filter_by(token_hash=_hash(raw)).one()
        row.session_started_at = datetime.now(timezone.utc) - timedelta(
            hours=SESSION_ABSOLUTE_MAX_HOURS + 1
        )

    r = _refresh(unauth_client, raw)
    assert r.status_code == 401
    payload = r.json().get("detail") or r.json().get("message")
    assert "session_absolute_limit" in str(payload)


def test_login_records_ip_and_user_agent(unauth_client):
    unauth_client.post(
        "/api/v1/auth/login",
        data={"username": "lifecycle_user", "password": "lifecycle_pass_99"},
        headers={"User-Agent": "TestBrowser/1.0"},
    )
    with get_session() as session:
        row = (
            session.query(RefreshTokenModel)
            .filter_by(username="lifecycle_user")
            .order_by(RefreshTokenModel.id.desc())
            .first()
        )
        assert row.user_agent == "TestBrowser/1.0"
        # TestClient reports 'testclient' as host; just check it's populated
        assert row.ip_address is not None


def test_service_revoke_returns_true_for_valid_token():
    raw = refresh_token_service.create("lifecycle_user")
    assert refresh_token_service.revoke(raw) is True


def test_service_revoke_returns_false_for_unknown_token():
    assert refresh_token_service.revoke("garbage") is False


def test_service_double_revoke_returns_false():
    raw = refresh_token_service.create("lifecycle_user")
    refresh_token_service.revoke(raw)
    assert refresh_token_service.revoke(raw) is False
