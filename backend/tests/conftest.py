import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.main import app


def _make_client(role: str) -> TestClient:
    token = create_access_token({"sub": role, "role": role})
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


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
    """Prevent real Ansible playbook execution in unit tests.

    Returns rc=1 for fail_device so failure-path tests still pass.
    """
    from app.services import ansible_service

    def _fake_run_playbook(playbook: str, extravars: dict, inventory: str | None = None) -> dict:
        device = extravars.get("device", "")
        if device == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
        return {"rc": 0, "stdout": "Simulated playbook output", "stderr": ""}

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run_playbook)
