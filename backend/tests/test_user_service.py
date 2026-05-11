"""Tests for user_service.py — CRUD, password ops, auth, last-admin guard."""
import pytest

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


def _create(username="alice", role="operator", password="password123", email=None):
    return user_service.create_user(
        UserCreate(username=username, password=password, role=role, email=email)
    )


# ── create_user ───────────────────────────────────────────────────────────────

def test_create_user_returns_user_read():
    user = _create(username="alice", role="operator")
    assert user.username == "alice"
    assert user.role == "operator"
    assert user.is_active is True
    assert user.id is not None


def test_create_user_normalises_username_to_lowercase():
    user = _create(username="Alice")
    assert user.username == "alice"


def test_create_user_normalises_email_to_lowercase():
    user = _create(username="bob", email="Bob@Example.COM")
    assert user.email == "bob@example.com"


def test_create_user_duplicate_username_raises():
    _create(username="carol")
    with pytest.raises(ValueError, match="already taken"):
        _create(username="carol")


def test_create_user_duplicate_username_case_insensitive():
    _create(username="dave")
    with pytest.raises(ValueError, match="already taken"):
        _create(username="Dave")


def test_create_user_duplicate_email_raises():
    _create(username="eve", email="shared@example.com")
    with pytest.raises(ValueError, match="already registered"):
        _create(username="frank", email="shared@example.com")


def test_create_user_null_email_allowed_multiple_times():
    _create(username="grace", email=None)
    _create(username="heidi", email=None)  # should not raise


def test_create_user_does_not_expose_password():
    user = _create(username="ivan")
    assert not hasattr(user, "hashed_password")
    assert not hasattr(user, "password")


# ── get_by_id / get_by_username ───────────────────────────────────────────────

def test_get_by_id_returns_user():
    created = _create(username="judy")
    fetched = user_service.get_by_id(created.id)
    assert fetched is not None
    assert fetched.username == "judy"


def test_get_by_id_unknown_returns_none():
    assert user_service.get_by_id(99999) is None


def test_get_by_username_returns_user():
    _create(username="karen")
    fetched = user_service.get_by_username("karen")
    assert fetched is not None
    assert fetched.username == "karen"


def test_get_by_username_case_insensitive():
    _create(username="leo")
    assert user_service.get_by_username("Leo") is not None


def test_get_by_username_unknown_returns_none():
    assert user_service.get_by_username("nobody") is None


# ── list_users ────────────────────────────────────────────────────────────────

def test_list_users_returns_active_by_default():
    _create(username="mallory")
    _create(username="niobe")
    user_service.deactivate_user(user_service.get_by_username("niobe").id)

    users = user_service.list_users()
    names = {u.username for u in users}
    assert "mallory" in names
    assert "niobe" not in names


def test_list_users_include_inactive():
    _create(username="oscar")
    _create(username="peggy")
    user_service.deactivate_user(user_service.get_by_username("peggy").id)

    users = user_service.list_users(include_inactive=True)
    names = {u.username for u in users}
    assert "oscar" in names
    assert "peggy" in names


def test_list_users_empty_returns_empty_list():
    assert user_service.list_users() == []


# ── update_user ───────────────────────────────────────────────────────────────

def test_update_user_email():
    user = _create(username="quinn")
    updated = user_service.update_user(user.id, UserUpdate(email="new@example.com"))
    assert updated.email == "new@example.com"


def test_update_user_email_normalised():
    user = _create(username="ruth")
    updated = user_service.update_user(user.id, UserUpdate(email="UPPER@CASE.COM"))
    assert updated.email == "upper@case.com"


def test_update_user_role():
    user = _create(username="sam", role="operator")
    updated = user_service.update_user(user.id, UserUpdate(role="admin"))
    assert updated.role == "admin"


def test_update_user_password_hashed():
    user = _create(username="tina", password="oldpass1")
    user_service.update_user(user.id, UserUpdate(password="newpass99"))
    assert user_service.verify_password("tina", "newpass99") is True
    assert user_service.verify_password("tina", "oldpass1") is False


def test_update_user_not_found_raises():
    with pytest.raises(ValueError, match="not found"):
        user_service.update_user(99999, UserUpdate(email="x@example.com"))


def test_update_user_email_clash_raises():
    _create(username="uma", email="taken@example.com")
    other = _create(username="vince")
    with pytest.raises(ValueError, match="already registered"):
        user_service.update_user(other.id, UserUpdate(email="taken@example.com"))


def test_update_user_same_email_no_clash():
    user = _create(username="wendy", email="wendy@example.com")
    updated = user_service.update_user(user.id, UserUpdate(email="wendy@example.com"))
    assert updated.email == "wendy@example.com"


# ── deactivate_user ───────────────────────────────────────────────────────────

def test_deactivate_user_sets_is_active_false():
    user = _create(username="xena")
    result = user_service.deactivate_user(user.id)
    assert result.is_active is False


def test_deactivate_last_admin_raises():
    user = _create(username="yvonne", role="admin")
    with pytest.raises(ValueError, match="last active admin"):
        user_service.deactivate_user(user.id)


def test_deactivate_non_last_admin_allowed():
    a1 = _create(username="admin1", role="admin")
    a2 = _create(username="admin2", role="admin")
    result = user_service.deactivate_user(a1.id)
    assert result.is_active is False


def test_deactivate_operator_allowed():
    user = _create(username="zara", role="operator")
    result = user_service.deactivate_user(user.id)
    assert result.is_active is False


# ── reactivate via update_user ────────────────────────────────────────────────

def test_reactivate_user():
    user = _create(username="adam")
    user_service.deactivate_user(user.id)
    result = user_service.update_user(user.id, UserUpdate(is_active=True))
    assert result.is_active is True


# ── verify_password ───────────────────────────────────────────────────────────

def test_verify_password_correct():
    _create(username="beth", password="mypassword1")
    assert user_service.verify_password("beth", "mypassword1") is True


def test_verify_password_wrong():
    _create(username="carl", password="mypassword1")
    assert user_service.verify_password("carl", "wrongpassword") is False


def test_verify_password_inactive_user_returns_false():
    user = _create(username="diana", password="mypassword1")
    user_service.deactivate_user(user.id)
    assert user_service.verify_password("diana", "mypassword1") is False


def test_verify_password_unknown_user_returns_false():
    assert user_service.verify_password("ghost", "anything") is False


# ── update_password ───────────────────────────────────────────────────────────

def test_update_password_works():
    user = _create(username="eli", password="firstpass1")
    user_service.update_password(user.id, "newpassword1")
    assert user_service.verify_password("eli", "newpassword1") is True


def test_update_password_too_short_raises():
    user = _create(username="fiona")
    with pytest.raises(ValueError, match="at least 8"):
        user_service.update_password(user.id, "short")


def test_update_password_unknown_user_raises():
    with pytest.raises(ValueError, match="not found"):
        user_service.update_password(99999, "validpassword")


# ── authenticate ──────────────────────────────────────────────────────────────

def test_authenticate_valid_credentials_returns_user_read():
    _create(username="gary", password="mypassword1")
    result = user_service.authenticate("gary", "mypassword1")
    assert result is not None
    assert result.username == "gary"


def test_authenticate_wrong_password_returns_none():
    _create(username="hank", password="mypassword1")
    assert user_service.authenticate("hank", "wrongpassword") is None


def test_authenticate_inactive_user_returns_none():
    user = _create(username="iris", password="mypassword1")
    user_service.deactivate_user(user.id)
    assert user_service.authenticate("iris", "mypassword1") is None


def test_authenticate_unknown_user_returns_none():
    assert user_service.authenticate("nobody", "anything") is None


def test_authenticate_does_not_expose_password():
    _create(username="jack", password="mypassword1")
    result = user_service.authenticate("jack", "mypassword1")
    assert result is not None
    assert not hasattr(result, "hashed_password")
