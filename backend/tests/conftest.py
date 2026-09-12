import os

os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "True")

# DATABASE_URL is left to the caller if it's already set. The default used
# to be a throwaway SQLite file, but a real migration
# (n8msp13_ntp_dns_log_lists.py) uses Postgres-only raw SQL (ALTER COLUMN
# ... TYPE json USING to_jsonb(...)) that SQLite can't run -- `alembic
# upgrade head` always failed past that point. Point at the dedicated
# Postgres test DB instead (already exists, reachable on the port
# docker-compose already exposes) -- the "test" name guard below still
# protects it.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://ansiauth:ansiauth_dev_password@localhost:5432/ansiauth_test",
)
os.environ["JWT_SECRET_KEY"] = "test_jwt_secret_key_not_for_production"
# Tests run over http://testserver, so the Secure cookie attribute would cause
# Starlette's TestClient (and any real browser) to refuse the refresh-token
# cookie. Mirror the local-dev override that .env.example documents.
os.environ.setdefault("COOKIE_SECURE", "false")

# Schema is owned by Alembic now that the startup DDL block in main.py was
# removed. Wipe any stale state from a previous interrupted run, then run
# migrations from scratch before app.main imports the session.
_db_url = os.environ["DATABASE_URL"]
if _db_url.startswith("sqlite") and "://./" in _db_url.replace("sqlite:///", "sqlite:///./"):
    if os.path.exists("./test.db"):
        os.remove("./test.db")
elif _db_url.startswith("postgresql"):
    # Postgres: drop every table in the public schema so each test run starts
    # from a known-empty state. Cheaper than dropping the database itself —
    # avoids needing a separate admin connection.
    #
    # Guard added after a real incident: DATABASE_URL is set unconditionally
    # by docker-compose on the `backend`/`worker` services, pointing at the
    # actual dev database -- `setdefault()` above is a no-op there, so
    # running pytest *inside* those containers silently DROP SCHEMA CASCADE'd
    # the real dev DB (all devices/jobs/audit history, gone, no backup
    # existed). Refuse to touch anything whose database name doesn't look
    # like a disposable test DB -- forces callers to point pytest at a
    # dedicated database instead of trusting whatever DATABASE_URL happens
    # to be set in the environment it's invoked from.
    from urllib.parse import urlparse as _urlparse
    _db_name = (_urlparse(_db_url).path or "").lstrip("/")
    if "test" not in _db_name.lower():
        raise RuntimeError(
            f"Refusing to run tests against database {_db_name!r} (from "
            f"DATABASE_URL) -- its name doesn't contain 'test'. This guard "
            f"exists because this exact mistake already wiped the real dev "
            f"database once (conftest.py runs DROP SCHEMA CASCADE). Point "
            f"DATABASE_URL at a dedicated test database, e.g. "
            f"postgresql://.../ansiauth_test, before running pytest."
        )
    import sqlalchemy as _sa
    _engine = _sa.create_engine(_db_url)
    with _engine.begin() as _conn:
        _conn.execute(_sa.text("DROP SCHEMA public CASCADE"))
        _conn.execute(_sa.text("CREATE SCHEMA public"))
    _engine.dispose()

from alembic import command as _alembic_command
from alembic.config import Config as _AlembicConfig

_alembic_cfg = _AlembicConfig(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
_alembic_command.upgrade(_alembic_cfg, "head")

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.main import app  # DB session is initialized inside main on import


def _make_client(role: str) -> TestClient:
    """Create a TestClient whose bearer token asserts the given role.

    ``role`` is one of ``observer|operator|admin|super-admin`` — mapped to
    ``is_system_admin`` for the JWT claim (admin/super-admin → True) so that
    fixtures preserve their pre-Phase-5 semantics: system-admin roles bypass
    scope checks, observer/operator roles are enriched by per-scope grants
    seeded in ``_seed_test_role_users_with_full_visibility``.
    """
    is_system_admin = role in {"admin", "super-admin"}
    token = create_access_token({"sub": role, "is_system_admin": is_system_admin})
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """Create tables once for the whole test session. Defaults (system-admin
    bootstrap + the Base Infrastructure Site/Default group) are no longer
    seeded here -- app.main already does that at module-import time (see
    ``with system_context(): _bootstrap_admin() ...`` in app/main.py), and
    the ``from app.main import app`` above already triggered it."""
    yield
    if os.path.exists("./test.db"):
        os.remove("./test.db")


@pytest.fixture
def client():
    return _make_client("admin")


@pytest.fixture
def admin_client():
    return _make_client("admin")


@pytest.fixture
def operator_client():
    return _make_client("operator")


@pytest.fixture
def observer_client():
    return _make_client("observer")


@pytest.fixture
def super_admin_client():
    return _make_client("super-admin")


@pytest.fixture
def unauth_client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_vlan_mock():
    from app.services.vendors.mock import reset_mock_vlans
    reset_mock_vlans()
    yield
    reset_mock_vlans()


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    from app.composition import redis_coordinator
    redis_coordinator.resetear()
    yield
    redis_coordinator.resetear()


@pytest.fixture(autouse=True)
def reset_login_attempts():
    from app.composition import login_attempt_repository
    login_attempt_repository.resetear_todo()
    yield
    login_attempt_repository.resetear_todo()


@pytest.fixture(autouse=True)
def reset_rate_limit_middleware():
    from app.core import rls_middleware as rl
    rl.reset()
    yield
    rl.reset()


@pytest.fixture(autouse=True)
def reset_refresh_tokens():
    from app.db.models import RefreshTokenModel
    from app.db.session import get_session
    with get_session() as session:
        session.query(RefreshTokenModel).delete(synchronize_session=False)
    yield
    with get_session() as session:
        session.query(RefreshTokenModel).delete(synchronize_session=False)


@pytest.fixture(autouse=True)
def _seed_test_role_users_with_full_visibility():
    """Seed DB users matching the synthetic-JWT test clients and grant
    observer/operator per-scope roles on every REGULAR site so require_scope
    decisions succeed by default. Tests that want restricted access can
    revoke grants explicitly. Admin/super-admin clients set
    ``is_system_admin=True`` in the JWT and bypass scope checks.
    """
    from app.db.models import (
        RoleAssignmentModel,
        SiteModel,
        UserModel,
    )
    from app.db.session import get_session
    from app.composition import user_repository

    # Match passwords other test modules already expect, so seed_users-style
    # fixtures (test_audit.py, test_auth.py) that only create-if-missing find
    # a user with the right credentials.
    _accounts = [
        ("admin", "admin123", True),
        ("operator", "operator123", False),
        ("observer", "observer123", False),
        ("super-admin", "superadmin123", True),
    ]
    for username, password, is_sys_admin in _accounts:
        if user_repository.obtener_por_username(username) is None:
            user_repository.crear(username, password, is_system_admin=is_sys_admin)

    with get_session() as session:
        # Per D14, Base Infra is only visible to system-admins — grant
        # observer/operator on every REGULAR site to preserve pre-MSP
        # semantics of "every site".
        all_site_ids = [
            r[0] for r in session.query(SiteModel.id).filter(
                SiteModel.kind == "REGULAR"
            ).all()
        ]
        for role in ("observer", "operator"):
            user_row = session.query(UserModel).filter_by(username=role).first()
            if user_row is None:
                continue
            session.query(RoleAssignmentModel).filter(
                RoleAssignmentModel.user_id == user_row.id,
                RoleAssignmentModel.device_group_id.is_(None),
                RoleAssignmentModel.site_id.in_(all_site_ids) if all_site_ids else False,
            ).delete(synchronize_session=False)
            for sid in all_site_ids:
                session.add(RoleAssignmentModel(
                    user_id=user_row.id,
                    site_id=sid,
                    device_group_id=None,
                    role=role,
                ))
    yield


@pytest.fixture(autouse=True)
def mock_ansible_service(monkeypatch):
    """Prevent real Ansible playbook execution in unit tests. The VLAN-mock
    patching this fixture used to also do (get_vlans/vlan_exists/delete_vlan
    on the deleted vlan_service module) is gone -- that in-memory mock state
    now lives on app.services.vendors.mock (MockVendor), reset per-test by
    the reset_vlan_mock fixture above; nothing here needs to touch it."""
    from app.services import ansible_service

    def _fake_run_playbook(playbook: str, extravars: dict, inventory: str | None = None, device: str | None = None) -> dict:
        dev = device or extravars.get("device", "")
        if dev == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
        return {"rc": 0, "stdout": "Simulated playbook output", "stderr": ""}

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run_playbook)
