import os

os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "True")

# DATABASE_URL is left to the caller if it's already set (Step 6 lets the
# whole suite run against a real Postgres container — see
# docs/refactor-steps/step-6-postgres-portable.md). The default keeps the
# historical behaviour: a throwaway SQLite file in the cwd.
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
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
from app.services import device_service


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
    """Create tables and seed defaults once for the whole test session."""
    device_service.seed_defaults()
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
    from app.services import vlan_service
    vlan_service.reset_mock_vlans()
    yield
    vlan_service.reset_mock_vlans()


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    from app.services import rate_limiter
    rate_limiter.reset()
    yield
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def reset_login_attempts():
    from app.services import login_attempt_service
    login_attempt_service.reset_all()
    yield
    login_attempt_service.reset_all()


@pytest.fixture(autouse=True)
def reset_rate_limit_middleware():
    from app.core import rate_limit_middleware as rl
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
    from app.services import user_service
    from app.schemas.user import UserCreate

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
        if user_service.get_by_username(username) is None:
            user_service.create_user(UserCreate(
                username=username,
                password=password,
                is_system_admin=is_sys_admin,
            ))

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
    """Prevent real Ansible playbook execution in unit tests."""
    from app.services import ansible_service, vlan_service

    def _fake_run_playbook(playbook: str, extravars: dict, inventory: str | None = None, device: str | None = None) -> dict:
        dev = device or extravars.get("device", "")
        if dev == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
        return {"rc": 0, "stdout": "Simulated playbook output", "stderr": ""}

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run_playbook)
    # Patch get_vlans to return current in-memory mock state — prevents any get_vlans.yml
    # Ansible call in tests that patch EXECUTION_MODE="real". Override per-test as needed.
    monkeypatch.setattr(vlan_service, "get_vlans", lambda device_id=None: vlan_service._mock_vlans[:])
    # Keep vlan_exists patched as well for tests that still reference it directly.
    monkeypatch.setattr(vlan_service, "vlan_exists", lambda device_id, vlan_id: False)

    # Patch delete_vlan so post-deletion verification in run_delete_job sees the updated
    # _mock_vlans state. Delegates to the current run_playbook (which may be overridden
    # per-test) so that retry/rollback tests that patch run_playbook still get rc=1.
    def _fake_delete_vlan(vlan_id: int, device_id: str) -> dict:
        result = ansible_service.run_playbook(
            "delete_vlan.yml",
            extravars={"vlan_id": vlan_id, "device": device_id},
            device=device_id,
        )
        if result["rc"] == 0:
            vlan_service._mock_vlans[:] = [v for v in vlan_service._mock_vlans if v.vlan_id != vlan_id]
        return result

    monkeypatch.setattr(vlan_service, "delete_vlan", _fake_delete_vlan)
