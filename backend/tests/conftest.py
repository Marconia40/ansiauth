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
