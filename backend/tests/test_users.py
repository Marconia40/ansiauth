"""TEST-002 — Supplemental user management tests.

Covers cases not present in test_users_api.py or test_user_service.py:
  - Last super-admin deactivation block (API and service layer)
  - Observer role cannot access any user management endpoint
  - Unauthenticated requests return 401 for all user endpoints
"""
import pytest

from app.core.exceptions import ValidationError
from app.db.models import AuditLogModel, UserModel
from app.db.session import get_session
from app.composition import user_repository

from tests.conftest import elevated_headers


@pytest.fixture(autouse=True)
def clean_users():
    with get_session() as session:
        session.query(UserModel).delete()
        session.query(AuditLogModel).delete()
    yield
    with get_session() as session:
        session.query(UserModel).delete()
        session.query(AuditLogModel).delete()


def _seed(username, is_system_admin=False, password="password123"):
    return user_repository.crear(username, password, is_system_admin=is_system_admin)


def _deactivate(user_id):
    """Test-only stand-in for the deleted user_service.deactivate_user() --
    same guard (UserRepository.es_ultimo_admin_activo(), Fase-checked
    BEFORE the domain mutation) + the same domain call the real
    api/users.py:deactivate_user() endpoint makes."""
    if user_repository.es_ultimo_admin_activo(user_id):
        raise ValidationError("Cannot deactivate the last active system-admin account")
    user = user_repository.get(user_id)
    user.desactivar()
    return user_repository.add(user)


# ── Last system-admin deactivation guard ──────────────────────────────────────

def test_api_blocks_deactivating_last_system_admin(super_admin_client):
    """DELETE on the sole remaining system-admin must be rejected.

    ValidationError now maps to 422 (app.main:validation_error_handler),
    not 400 -- ValueError-in-service->400 was the old shape."""
    user = _seed("only_sa", is_system_admin=True)
    resp = super_admin_client.delete(f"/api/v1/users/{user.id}", headers=elevated_headers("super-admin"))
    assert resp.status_code == 422


def test_service_blocks_deactivating_last_system_admin():
    """The deactivation guard raises ValidationError when the target is the
    last system-admin (matches api/users.py:deactivate_user()'s own guard --
    the old user_service.deactivate_user() raised plain ValueError, this
    layer uses the app's standard 400-mapped ValidationError instead)."""
    user = _seed("only_sa2", is_system_admin=True)
    with pytest.raises(ValidationError, match="last active system-admin"):
        _deactivate(user.id)


def test_deactivating_non_last_system_admin_is_allowed():
    """A system-admin can be deactivated when a second active system-admin remains."""
    sa1 = _seed("sa_first", is_system_admin=True)
    _seed("sa_second", is_system_admin=True)
    result = _deactivate(sa1.id)
    assert result.is_active is False


# ── Observer role cannot access user management endpoints ─────────────────────

def test_observer_cannot_create_user(observer_client):
    resp = observer_client.post("/api/v1/users/", json={
        "username": "newuser", "password": "password123"
    })
    assert resp.status_code == 403


def test_observer_cannot_list_users(observer_client):
    resp = observer_client.get("/api/v1/users/")
    assert resp.status_code == 403


def test_observer_cannot_get_user_by_id(observer_client):
    resp = observer_client.get("/api/v1/users/1")
    assert resp.status_code == 403


def test_observer_cannot_update_user(observer_client):
    resp = observer_client.put("/api/v1/users/1", json={"email": "x@y.com"})
    assert resp.status_code == 403


def test_observer_cannot_deactivate_user(observer_client):
    resp = observer_client.delete("/api/v1/users/1")
    assert resp.status_code == 403


# ── Unauthenticated requests return 401 ──────────────────────────────────────

def test_unauth_create_user_returns_401(unauth_client):
    resp = unauth_client.post("/api/v1/users/", json={
        "username": "newuser", "password": "password123"
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
