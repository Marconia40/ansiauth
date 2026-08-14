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
