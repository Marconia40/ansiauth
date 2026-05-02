import os

# Must be set before app imports so config.py picks up the test URL.
os.environ["DATABASE_URL"] = "sqlite:///./test.db"

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
def unauth_client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def mock_ansible_service(monkeypatch):
    """Prevent real Ansible playbook execution in unit tests."""
    from app.services import ansible_service

    def _fake_run_playbook(playbook: str, extravars: dict, inventory: str | None = None, device: str | None = None) -> dict:
        dev = device or extravars.get("device", "")
        if dev == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
        return {"rc": 0, "stdout": "Simulated playbook output", "stderr": ""}

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run_playbook)
