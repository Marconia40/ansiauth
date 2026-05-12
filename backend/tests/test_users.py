"""TEST-002 — Supplemental user management tests.

Covers cases not present in test_users_api.py or test_user_service.py:
  - Last super-admin deactivation block (API and service layer)
  - Observer role cannot access any user management endpoint
  - Unauthenticated requests return 401 for all user endpoints
"""
import pytest

from app.db.models import AuditLogModel, UserModel
from app.db.session import get_session
from app.schemas.user import UserCreate
from app.services import user_service


@pytest.fixture(autouse=True)
def clean_users():
    with get_session() as session:
        session.query(UserModel).delete()
        session.query(AuditLogModel).delete()
    yield
    with get_session() as session:
        session.query(UserModel).delete()
        session.query(AuditLogModel).delete()


def _seed(username, role="operator", password="password123"):
    return user_service.create_user(
        UserCreate(username=username, password=password, role=role)
    )


# ── Last super-admin deactivation guard ───────────────────────────────────────

def test_api_blocks_deactivating_last_super_admin(super_admin_client):
    """DELETE on the sole remaining super-admin must return 400."""
    user = _seed("only_sa", role="super-admin")
    resp = super_admin_client.delete(f"/api/v1/users/{user.id}")
    assert resp.status_code == 400


def test_service_blocks_deactivating_last_super_admin():
    """deactivate_user() raises ValueError when the target is the last super-admin."""
    user = _seed("only_sa2", role="super-admin")
    with pytest.raises(ValueError, match="last active super-admin"):
        user_service.deactivate_user(user.id)


def test_deactivating_non_last_super_admin_is_allowed():
    """A super-admin can be deactivated when a second active super-admin remains."""
    sa1 = _seed("sa_first", role="super-admin")
    _seed("sa_second", role="super-admin")
    result = user_service.deactivate_user(sa1.id)
    assert result.is_active is False


# ── Observer role cannot access user management endpoints ─────────────────────

def test_observer_cannot_create_user(observer_client):
    resp = observer_client.post("/api/v1/users/", json={
        "username": "newuser", "password": "password123", "role": "operator"
    })
    assert resp.status_code == 403


def test_observer_cannot_list_users(observer_client):
    resp = observer_client.get("/api/v1/users/")
    assert resp.status_code == 403


def test_observer_cannot_get_user_by_id(observer_client):
    resp = observer_client.get("/api/v1/users/1")
    assert resp.status_code == 403


def test_observer_cannot_update_user(observer_client):
    resp = observer_client.put("/api/v1/users/1", json={"role": "operator"})
    assert resp.status_code == 403


def test_observer_cannot_deactivate_user(observer_client):
    resp = observer_client.delete("/api/v1/users/1")
    assert resp.status_code == 403


# ── Unauthenticated requests return 401 ──────────────────────────────────────

def test_unauth_create_user_returns_401(unauth_client):
    resp = unauth_client.post("/api/v1/users/", json={
        "username": "newuser", "password": "password123", "role": "operator"
    })
    assert resp.status_code == 401


def test_unauth_list_users_returns_401(unauth_client):
    resp = unauth_client.get("/api/v1/users/")
    assert resp.status_code == 401


def test_unauth_get_user_returns_401(unauth_client):
    resp = unauth_client.get("/api/v1/users/1")
    assert resp.status_code == 401


def test_unauth_update_user_returns_401(unauth_client):
    resp = unauth_client.put("/api/v1/users/1", json={"role": "operator"})
    assert resp.status_code == 401


def test_unauth_deactivate_user_returns_401(unauth_client):
    resp = unauth_client.delete("/api/v1/users/1")
    assert resp.status_code == 401
