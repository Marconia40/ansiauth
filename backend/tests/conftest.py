import os

# Must be set before app imports so config.py picks up these values.
os.environ["DATABASE_URL"] = "sqlite:///./test.db"
os.environ["JWT_SECRET_KEY"] = "test_jwt_secret_key_not_for_production"

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.main import app  # DB is initialized inside main on import
from app.services import device_service


def _make_client(role: str) -> TestClient:
    token = create_access_token({"sub": role, "role": role})
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
    """Step 7.3 — site-scoped RBAC looks up the calling user by username.

    The role-based test clients use synthetic JWTs (sub == role), so we seed real
    DB users for those usernames and grant non-admin roles access to every
    existing site. Tests that want to verify restricted access can revoke sites
    explicitly. Admins/super-admins bypass scoping by policy and don't need
    allowed_sites rows.
    """
    from app.db.models import SiteModel, UserAllowedSiteModel, UserModel
    from app.db.session import get_session
    from app.services import user_service
    from app.schemas.user import UserCreate

    # Match passwords other test modules already expect, so seed_users-style
    # fixtures (test_audit.py, test_auth.py) that only create-if-missing find a
    # user with the right credentials.
    _accounts = [
        ("admin", "admin123"),
        ("operator", "operator123"),
        ("observer", "observer123"),
        ("super-admin", "superadmin123"),
    ]
    for role, password in _accounts:
        if user_service.get_by_username(role) is None:
            user_service.create_user(UserCreate(
                username=role,
                password=password,
                role=role,
            ))

    with get_session() as session:
        all_site_ids = [r[0] for r in session.query(SiteModel.id).all()]
        for role in ("observer", "operator"):
            user_row = session.query(UserModel).filter_by(username=role).first()
            if user_row is None:
                continue
            session.query(UserAllowedSiteModel).filter_by(user_id=user_row.id).delete(
                synchronize_session=False
            )
            for sid in all_site_ids:
                session.add(UserAllowedSiteModel(user_id=user_row.id, site_id=sid))
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
