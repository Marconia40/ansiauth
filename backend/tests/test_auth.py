import pytest

from app.db.models import UserModel
from app.db.session import get_session
from app.schemas.user import UserCreate
from app.services import user_service


# ── Seed DB users that auth tests rely on ─────────────────────────────────────

@pytest.fixture(autouse=True)
def seed_auth_users():
    """Reset the three test accounts with known passwords before each auth test.

    MSP: Phase 4 — deleting a user cascades their ``role_assignments`` away,
    which erases the observer/operator grants seeded by conftest. Re-seed
    grants on every REGULAR site so tests that authenticate as observer/
    operator can still hit /vlans and /audit through effective_role.
    """
    from app.db.models import RoleAssignmentModel, SiteModel, UserAllowedSiteModel
    _accounts = [
        ("admin", "admin123", "admin"),
        ("operator", "operator123", "operator"),
        ("observer", "observer123", "observer"),
    ]
    with get_session() as session:
        session.query(UserModel).filter(
            UserModel.username.in_([u for u, _, _ in _accounts])
        ).delete(synchronize_session=False)
    for username, password, role in _accounts:
        user_service.create_user(UserCreate(username=username, password=password, role=role))
    with get_session() as session:
        regular_site_ids = [
            r[0] for r in session.query(SiteModel.id).filter(
                SiteModel.kind == "REGULAR"
            ).all()
        ]
        for role in ("observer", "operator"):
            row = session.query(UserModel).filter_by(username=role).first()
            if row is None:
                continue
            for sid in regular_site_ids:
                session.add(UserAllowedSiteModel(user_id=row.id, site_id=sid))
                session.add(RoleAssignmentModel(
                    user_id=row.id, site_id=sid,
                    device_group_id=None, role=role,
                ))
    yield
    with get_session() as session:
        session.query(UserModel).filter(
            UserModel.username.in_([u for u, _, _ in _accounts])
        ).delete(synchronize_session=False)


# ── Login ─────────────────────────────────────────────────────────────────────

def test_login_success(unauth_client):
    response = unauth_client.post("/api/v1/auth/login", data={"username": "admin", "password": "admin123"})

    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


def test_login_invalid_credentials(unauth_client):
    response = unauth_client.post("/api/v1/auth/login", data={"username": "admin", "password": "wrongpass"})

    assert response.status_code == 401


def test_login_unknown_user_returns_401(unauth_client):
    response = unauth_client.post("/api/v1/auth/login", data={"username": "ghost", "password": "anything"})

    assert response.status_code == 401


def test_login_inactive_user_returns_401(unauth_client):
    user = user_service.get_by_username("observer")
    user_service.deactivate_user(user.id)

    response = unauth_client.post("/api/v1/auth/login", data={"username": "observer", "password": "observer123"})

    assert response.status_code == 401


def test_login_then_use_token(unauth_client):
    login_response = unauth_client.post(
        "/api/v1/auth/login", data={"username": "operator", "password": "operator123"}
    )
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]

    vlan_response = unauth_client.get(
        "/api/v1/vlans/?device=mock_device",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert vlan_response.status_code == 200
    assert isinstance(vlan_response.json()["data"], list)


# ── Token / access guards ─────────────────────────────────────────────────────

def test_access_without_token(unauth_client):
    response = unauth_client.get("/api/v1/vlans/")

    assert response.status_code == 401


def test_access_with_invalid_token(unauth_client):
    response = unauth_client.get(
        "/api/v1/vlans/",
        headers={"Authorization": "Bearer this.is.not.a.valid.token"},
    )

    assert response.status_code == 401


def test_observer_cannot_create_vlan(observer_client):
    payload = {"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]}

    response = observer_client.post("/api/v1/vlans/", json=payload)

    assert response.status_code == 403


def test_operator_can_create_vlan(operator_client):
    payload = {"vlan_id": 50, "name": "TEST", "devices": ["mock_device"]}

    response = operator_client.post("/api/v1/vlans/", json=payload)

    assert response.status_code == 200


def test_admin_can_delete_vlan(admin_client):
    response = admin_client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})

    assert response.status_code == 200


def test_operator_cannot_delete_vlan(operator_client):
    response = operator_client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})

    assert response.status_code == 403


def test_token_accepted_within_expiry_window(unauth_client):
    import os
    import jwt as jose_jwt  # name retained for blame-stable diff; PyJWT has the same encode/decode signatures
    from datetime import datetime, timezone, timedelta

    token = jose_jwt.encode(
        {
            "sub": "admin",
            "role": "admin",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
        },
        os.environ["JWT_SECRET_KEY"],
        algorithm="HS256",
    )
    response = unauth_client.get("/api/v1/vlans/?device=mock_device", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_token_rejected_after_expiry(unauth_client):
    import os
    import jwt as jose_jwt  # name retained for blame-stable diff; PyJWT has the same encode/decode signatures
    from datetime import datetime, timezone, timedelta

    expired_token = jose_jwt.encode(
        {
            "sub": "admin",
            "role": "admin",
            "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
        },
        os.environ["JWT_SECRET_KEY"],
        algorithm="HS256",
    )
    response = unauth_client.get("/api/v1/vlans/", headers={"Authorization": f"Bearer {expired_token}"})
    assert response.status_code == 401


def test_startup_fails_when_expiry_exceeds_ceiling():
    import importlib
    import app.core.config as cfg
    import app.core.security as sec

    original = cfg.ACCESS_TOKEN_EXPIRE_MINUTES
    cfg.ACCESS_TOKEN_EXPIRE_MINUTES = 61
    try:
        with pytest.raises(RuntimeError, match="ACCESS_TOKEN_EXPIRE_MINUTES must be between"):
            importlib.reload(sec)
    finally:
        cfg.ACCESS_TOKEN_EXPIRE_MINUTES = original
        importlib.reload(sec)


def test_startup_fails_when_expiry_is_zero():
    import importlib
    import app.core.config as cfg
    import app.core.security as sec

    original = cfg.ACCESS_TOKEN_EXPIRE_MINUTES
    cfg.ACCESS_TOKEN_EXPIRE_MINUTES = 0
    try:
        with pytest.raises(RuntimeError, match="ACCESS_TOKEN_EXPIRE_MINUTES must be between"):
            importlib.reload(sec)
    finally:
        cfg.ACCESS_TOKEN_EXPIRE_MINUTES = original
        importlib.reload(sec)
