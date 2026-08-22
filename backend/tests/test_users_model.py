"""Tests for the UserModel ORM and UserCreate/UserRead/UserUpdate schemas."""
import pytest
import sqlalchemy.exc

from app.db.models import UserModel
from app.db.session import get_session
from app.schemas.user import UserCreate, UserRead, UserUpdate


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_users():
    """Remove all user rows before and after each test for isolation."""
    with get_session() as session:
        session.query(UserModel).delete()
    yield
    with get_session() as session:
        session.query(UserModel).delete()


def _make_user(username="alice", is_system_admin=False, email=None):
    with get_session() as session:
        row = UserModel(
            username=username,
            hashed_password="$2b$12$fakehash",
            is_system_admin=is_system_admin,
            email=email,
        )
        session.add(row)
        session.flush()
        user_id = row.id
    return user_id


# ── ORM model tests ───────────────────────────────────────────────────────────


def test_user_row_stored_and_retrievable():
    """A user row written to the DB can be read back with all fields."""
    _make_user(username="bob", is_system_admin=True, email="bob@example.com")

    with get_session() as session:
        row = session.query(UserModel).filter_by(username="bob").first()
        assert row is not None
        assert row.is_system_admin is True
        assert row.email == "bob@example.com"
        assert row.is_active is True
        assert row.created_at is not None
        assert row.updated_at is not None


def test_is_active_defaults_to_true():
    """is_active must default to True when not explicitly set."""
    _make_user(username="carol")

    with get_session() as session:
        row = session.query(UserModel).filter_by(username="carol").first()
        assert row.is_active is True


def test_duplicate_username_raises():
    """The unique constraint on username must reject a second row with the same value."""
    _make_user(username="dave")

    with pytest.raises((sqlalchemy.exc.IntegrityError, sqlalchemy.exc.OperationalError)):
        _make_user(username="dave")


def test_duplicate_email_raises():
    """The unique constraint on email must reject a second row with the same value."""
    _make_user(username="eve", email="shared@example.com")

    with pytest.raises((sqlalchemy.exc.IntegrityError, sqlalchemy.exc.OperationalError)):
        _make_user(username="frank", email="shared@example.com")


def test_null_email_allowed_for_multiple_rows():
    """NULL email must not trigger the unique constraint — NULL != NULL per SQL standard."""
    _make_user(username="grace", email=None)
    _make_user(username="heidi", email=None)  # should not raise


def test_soft_delete_via_is_active():
    """Setting is_active=False deactivates a user without deleting the row."""
    _make_user(username="ivan")

    with get_session() as session:
        row = session.query(UserModel).filter_by(username="ivan").first()
        row.is_active = False

    with get_session() as session:
        row = session.query(UserModel).filter_by(username="ivan").first()
        assert row.is_active is False
        assert row.username == "ivan"


# ── Schema tests ──────────────────────────────────────────────────────────────


def test_user_create_valid():
    schema = UserCreate(username="judy", password="secret123")
    assert schema.username == "judy"
    assert schema.is_system_admin is False
    assert schema.email is None


def test_user_create_accepts_is_system_admin():
    schema = UserCreate(username="root", password="secret123", is_system_admin=True)
    assert schema.is_system_admin is True


def test_user_create_rejects_short_username():
    with pytest.raises(Exception):
        UserCreate(username="ab", password="secret123")


def test_user_create_rejects_short_password():
    with pytest.raises(Exception):
        UserCreate(username="niobe", password="short")


def test_user_update_all_optional():
    """UserUpdate with no fields must not raise — all fields are optional."""
    schema = UserUpdate()
    assert schema.email is None
    assert schema.is_active is None
    assert schema.password is None


def test_user_read_shape():
    """UserRead must expose expected fields and exclude hashed_password."""
    fields = UserRead.model_fields
    assert "id" in fields
    assert "username" in fields
    assert "email" in fields
    assert "is_system_admin" in fields
    assert "is_active" in fields
    assert "created_at" in fields
    assert "updated_at" in fields
    assert "hashed_password" not in fields


# ── Migration round-trip test ─────────────────────────────────────────────────


def test_users_table_exists_in_db():
    """The users table must be present — proves the migration was applied."""
    from sqlalchemy import inspect as sa_inspect
    from app.db.session import get_engine
    inspector = sa_inspect(get_engine())
    assert "users" in inspector.get_table_names()
    col_names = {c["name"] for c in inspector.get_columns("users")}
    expected = {"id", "username", "email", "hashed_password", "is_system_admin",
                "is_active", "created_at", "updated_at"}
    assert expected.issubset(col_names)
