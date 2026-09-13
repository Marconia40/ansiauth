import pytest

from app.composition import (
    device_repository, plugin_registry, role_assignment_repository,
    site_repository, user_repository,
)
from app.db.models import SiteModel, UserModel
from app.db.session import get_session
from app.repositories.role_assignment_repository import RoleAssignment
from app.services.vendors.mock import MockVendor

_TEST_SITE_NAME = "AuthTestSite"


# ── Seed DB users that auth tests rely on ─────────────────────────────────────

@pytest.fixture(autouse=True)
def seed_auth_users():
    """Reset the three test accounts with known passwords before each auth
    test. Deleting a user cascades role_assignments away, which erases the
    observer/operator grants seeded by conftest — re-seed one grant per
    REGULAR site so tests that authenticate as observer/operator can still
    hit /vlans and /audit through their VisibilityScope."""
    from app.db.models import RoleAssignmentModel
    _accounts = [
        ("admin", "admin123", True),
        ("operator", "operator123", False),
        ("observer", "observer123", False),
    ]
    with get_session() as session:
        session.query(UserModel).filter(
            UserModel.username.in_([u for u, _, _ in _accounts])
        ).delete(synchronize_session=False)
    for username, password, is_sys in _accounts:
        user_repository.crear(username, password, is_system_admin=is_sys)
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
                session.add(RoleAssignmentModel(
                    user_id=row.id, site_id=sid,
                    device_group_id=None, role=role,
                ))
    yield
    with get_session() as session:
        session.query(UserModel).filter(
            UserModel.username.in_([u for u, _, _ in _accounts])
        ).delete(synchronize_session=False)


@pytest.fixture(autouse=True)
def _auth_test_infra(seed_auth_users, admin_client):
    """Registers mock_device (used by several tests below) and grants the
    freshly re-created operator/observer users (seed_auth_users above wipes
    and re-creates them, dropping any grant from a REGULAR site that didn't
    exist yet) access to it. Also forces cisco_ios/huawei_vrp onto
    MockVendor -- this repo's .env pins EXECUTION_MODE=real for local dev,
    which composition.py bakes into plugin_registry at import time, before
    any fixture here runs."""
    plugin_registry.registrar("cisco_ios", MockVendor())
    plugin_registry.registrar("huawei_vrp", MockVendor())

    with get_session() as session:
        row = session.query(SiteModel).filter_by(name=_TEST_SITE_NAME).first()
        site_id = row.id if row else None
    if site_id is None:
        site_id = site_repository.crear_con_grupo_default(_TEST_SITE_NAME).id

    # Grant on whichever site actually owns "mock_device" -- if another
    # test file's fixture already registered it under ITS OWN site (device
    # names are global; these fixtures run across many files sharing one
    # DB), granting only on `site_id` here would leave operator/observer
    # unable to see a device that landed elsewhere.
    existing = device_repository.get("mock_device")
    if existing is None:
        resp = admin_client.post("/api/v1/devices/", json={
            "name": "mock_device", "host": "10.0.0.1", "vendor": "cisco_ios",
            "username": "admin", "password": "admin", "site_id": site_id,
        })
        assert resp.status_code == 200, resp.text
        device_site_id = site_id
    else:
        device_site_id = existing.site_id

    for username in ("operator", "observer"):
        user = user_repository.obtener_por_username(username)
        if user is None:
            continue
        scope = role_assignment_repository.scope_de({"id": user.id, "is_system_admin": False})
        if scope.rol_para(device_site_id) != username:
            role_assignment_repository.add(RoleAssignment(user_id=user.id, site_id=device_site_id, role=username))
    yield


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
    user = user_repository.obtener_por_username("observer")
    user.desactivar()
    user_repository.add(user)

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
    # GET /vlans/?device=X now wraps the VLAN list in a SyncedResource
    # envelope (data.data / data.synced_at / ...) instead of returning it
    # bare at data -- cache-first reads (device_sync_service), not live SSH.
    assert isinstance(vlan_response.json()["data"]["data"], list)


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

    # VLAN writes are async now (job dispatch) -- 202 Accepted, not 200.
    assert response.status_code == 202


def test_admin_can_delete_vlan(admin_client):
    response = admin_client.request("DELETE", "/api/v1/vlans/10", json={"devices": ["mock_device"]})

    assert response.status_code == 202


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
