"""Tests for AUTH-003 — super-admin role in RBAC hierarchy."""
import pytest
from fastapi import HTTPException

from app.core.dependencies import require_role
from app.db.models import UserModel
from app.db.session import get_session
from app.schemas.user import UserCreate, UserUpdate
from app.services import user_service


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_users():
    with get_session() as session:
        session.query(UserModel).delete()
    yield
    with get_session() as session:
        session.query(UserModel).delete()


# ── Role hierarchy: require_role() logic ──────────────────────────────────────

def test_super_admin_passes_admin_check():
    check = require_role("admin")
    result = check({"username": "sa", "role": "super-admin"})
    assert result["role"] == "super-admin"


def test_super_admin_passes_operator_check():
    check = require_role("operator")
    result = check({"username": "sa", "role": "super-admin"})
    assert result["role"] == "super-admin"


def test_super_admin_passes_observer_check():
    check = require_role("observer")
    result = check({"username": "sa", "role": "super-admin"})
    assert result["role"] == "super-admin"


def test_super_admin_passes_super_admin_check():
    check = require_role("super-admin")
    result = check({"username": "sa", "role": "super-admin"})
    assert result["role"] == "super-admin"


def test_admin_is_rejected_on_super_admin_endpoint():
    check = require_role("super-admin")
    with pytest.raises(HTTPException) as exc_info:
        check({"username": "admin", "role": "admin"})
    assert exc_info.value.status_code == 403


def test_operator_is_rejected_on_super_admin_endpoint():
    check = require_role("super-admin")
    with pytest.raises(HTTPException) as exc_info:
        check({"username": "op", "role": "operator"})
    assert exc_info.value.status_code == 403


def test_observer_is_rejected_on_super_admin_endpoint():
    check = require_role("super-admin")
    with pytest.raises(HTTPException) as exc_info:
        check({"username": "obs", "role": "observer"})
    assert exc_info.value.status_code == 403


def test_admin_passes_admin_check():
    check = require_role("admin")
    result = check({"username": "admin", "role": "admin"})
    assert result["role"] == "admin"


def test_operator_is_rejected_on_admin_endpoint():
    check = require_role("admin")
    with pytest.raises(HTTPException) as exc_info:
        check({"username": "op", "role": "operator"})
    assert exc_info.value.status_code == 403


# ── HTTP endpoints: super-admin reaches all existing tiers ────────────────────

def test_super_admin_client_reaches_observer_endpoint(super_admin_client):
    response = super_admin_client.get("/api/v1/vlans/")
    assert response.status_code == 200


def test_super_admin_client_reaches_admin_endpoint(super_admin_client, admin_client):
    response = super_admin_client.get("/api/v1/audit/")
    assert response.status_code == 200


# ── Schema: super-admin is a valid role ───────────────────────────────────────

def test_user_create_accepts_super_admin_role():
    schema = UserCreate(username="boss", password="password123", role="super-admin")
    assert schema.role == "super-admin"


def test_user_update_accepts_super_admin_role():
    from app.schemas.user import UserUpdate
    schema = UserUpdate(role="super-admin")
    assert schema.role == "super-admin"


# ── Service: last-super-admin guard ──────────────────────────────────────────

def test_deactivate_last_super_admin_raises():
    user = user_service.create_user(
        UserCreate(username="superadmin", password="password123", role="super-admin")
    )
    with pytest.raises(ValueError, match="last active super-admin"):
        user_service.deactivate_user(user.id)


def test_deactivate_non_last_super_admin_allowed():
    sa1 = user_service.create_user(
        UserCreate(username="sa1", password="password123", role="super-admin")
    )
    user_service.create_user(
        UserCreate(username="sa2", password="password123", role="super-admin")
    )
    result = user_service.deactivate_user(sa1.id)
    assert result.is_active is False


def test_super_admin_in_valid_roles():
    assert "super-admin" in user_service.VALID_ROLES


def test_super_admin_in_role_hierarchy():
    from app.core.dependencies import ROLE_HIERARCHY
    assert "super-admin" in ROLE_HIERARCHY
    assert ROLE_HIERARCHY["super-admin"] > ROLE_HIERARCHY["admin"]


# ── DB: super-admin can be stored and retrieved ───────────────────────────────

def test_super_admin_persisted_to_db():
    user = user_service.create_user(
        UserCreate(username="dbsa", password="password123", role="super-admin")
    )
    fetched = user_service.get_by_id(user.id)
    assert fetched is not None
    assert fetched.role == "super-admin"
