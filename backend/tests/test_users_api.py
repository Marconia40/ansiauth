"""Tests for USR-003 — /api/v1/users CRUD endpoints."""
import pytest

from app.composition import user_repository
from app.db.models import AuditLogModel, UserModel
from app.db.session import get_session

from tests.conftest import elevated_headers


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_users():
    with get_session() as session:
        session.query(UserModel).delete()
        session.query(AuditLogModel).delete()
    yield
    with get_session() as session:
        session.query(UserModel).delete()
        session.query(AuditLogModel).delete()


def _seed(username, is_system_admin=False, password="password123"):
    return user_repository.crear(username, password, is_system_admin=is_system_admin)


def _deactivate(user):
    user.desactivar()
    return user_repository.add(user)


# ── POST /api/v1/users ────────────────────────────────────────────────────────

def test_super_admin_can_create_user(super_admin_client):
    resp = super_admin_client.post("/api/v1/users/", json={
        "username": "newuser", "password": "password123"
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["username"] == "newuser"
    assert "hashed_password" not in data


def test_admin_can_create_user(admin_client):
    resp = admin_client.post("/api/v1/users/", json={
        "username": "newuser", "password": "password123"
    })
    assert resp.status_code == 200


def test_admin_can_create_super_admin_under_msp(admin_client):
    """MSP: Phase 4 — the legacy admin vs super-admin distinction collapsed
    into ``is_system_admin`` (D24: every legacy admin becomes system-admin).
    An admin-role user is a system-admin and can therefore create other
    super-admins. The user-management privilege gate is the system-admin
    bit, not the specific role name."""
    resp = admin_client.post("/api/v1/users/", json={
        "username": "newsa", "password": "password123", "is_system_admin": True
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["is_system_admin"] is True


def test_super_admin_can_create_super_admin(super_admin_client):
    resp = super_admin_client.post("/api/v1/users/", json={
        "username": "newsa", "password": "password123", "is_system_admin": True
    })
    assert resp.status_code == 200
    assert resp.json()["data"]["is_system_admin"] is True


def test_operator_cannot_create_user(operator_client):
    resp = operator_client.post("/api/v1/users/", json={
        "username": "newuser", "password": "password123"
    })
    assert resp.status_code == 403


def test_create_duplicate_username_returns_422(super_admin_client):
    """ValidationError now maps to 422 (app.main:validation_error_handler),
    not the old 400."""
    _seed("existing")
    resp = super_admin_client.post("/api/v1/users/", json={
        "username": "existing", "password": "password123"
    })
    assert resp.status_code == 422


def test_create_user_writes_audit_log(super_admin_client, admin_client):
    super_admin_client.post("/api/v1/users/", json={
        "username": "audited", "password": "password123"
    })
    with get_session() as session:
        entry = session.query(AuditLogModel).filter_by(action="create_user").first()
        assert entry is not None
        assert entry.details["username"] == "audited"


def test_hashed_password_not_in_create_response(super_admin_client):
    resp = super_admin_client.post("/api/v1/users/", json={
        "username": "safeuser", "password": "password123"
    })
    assert "hashed_password" not in str(resp.json())


# ── GET /api/v1/users ─────────────────────────────────────────────────────────

def test_admin_can_list_users(admin_client):
    _seed("alice")
    _seed("bob")
    resp = admin_client.get("/api/v1/users/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    # list_users() now nests items/total/page/page_size under data (same
    # envelope shape GET /audit/ and GET /vlans/ use), not top-level.
    assert isinstance(body["data"]["items"], list)
    assert body["data"]["total"] >= 2


def test_super_admin_can_list_users(super_admin_client):
    resp = super_admin_client.get("/api/v1/users/")
    assert resp.status_code == 200


def test_operator_cannot_list_users(operator_client):
    resp = operator_client.get("/api/v1/users/")
    assert resp.status_code == 403


def test_list_excludes_inactive_by_default(admin_client):
    user = _seed("inactive_user")
    _deactivate(user)
    resp = admin_client.get("/api/v1/users/")
    names = [u["username"] for u in resp.json()["data"]["items"]]
    assert "inactive_user" not in names


def test_list_includes_inactive_when_flagged(admin_client):
    user = _seed("inactive_user")
    _deactivate(user)
    resp = admin_client.get("/api/v1/users/?include_inactive=true")
    names = [u["username"] for u in resp.json()["data"]["items"]]
    assert "inactive_user" in names


def test_list_pagination(admin_client):
    for i in range(5):
        _seed(f"pageuser{i}")
    resp = admin_client.get("/api/v1/users/?page=1&page_size=2")
    body = resp.json()["data"]
    assert len(body["items"]) == 2
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert body["total"] >= 5


def test_list_page_two(admin_client):
    for i in range(4):
        _seed(f"p2user{i}")
    resp1 = admin_client.get("/api/v1/users/?page=1&page_size=2")
    resp2 = admin_client.get("/api/v1/users/?page=2&page_size=2")
    ids_p1 = {u["id"] for u in resp1.json()["data"]["items"]}
    ids_p2 = {u["id"] for u in resp2.json()["data"]["items"]}
    assert ids_p1.isdisjoint(ids_p2)


def test_list_no_hashed_password(admin_client):
    _seed("leaktest")
    resp = admin_client.get("/api/v1/users/")
    assert "hashed_password" not in str(resp.json())


# ── GET /api/v1/users/{id} ────────────────────────────────────────────────────

def test_admin_can_get_user_by_id(admin_client):
    user = _seed("carol")
    resp = admin_client.get(f"/api/v1/users/{user.id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["username"] == "carol"


def test_get_nonexistent_user_returns_404(admin_client):
    resp = admin_client.get("/api/v1/users/99999")
    assert resp.status_code == 404


def test_operator_cannot_get_user(operator_client):
    resp = operator_client.get("/api/v1/users/1")
    assert resp.status_code == 403


def test_get_user_no_hashed_password(admin_client):
    user = _seed("dave")
    resp = admin_client.get(f"/api/v1/users/{user.id}")
    assert "hashed_password" not in str(resp.json())


# ── PUT /api/v1/users/{id} ────────────────────────────────────────────────────

def test_super_admin_can_update_user(super_admin_client):
    user = _seed("eve")
    resp = super_admin_client.put(f"/api/v1/users/{user.id}", json={"email": "eve@new.com"})
    assert resp.status_code == 200
    assert resp.json()["data"]["email"] == "eve@new.com"


def test_admin_can_update_user(admin_client):
    """Admin is system-admin under D24 so user updates are permitted."""
    user = _seed("frank")
    resp = admin_client.put(f"/api/v1/users/{user.id}", json={"email": "frank@new.com"})
    assert resp.status_code == 200, resp.text


def test_update_user_writes_audit_log(super_admin_client):
    user = _seed("grace")
    super_admin_client.put(f"/api/v1/users/{user.id}", json={"email": "grace@x.com"})
    with get_session() as session:
        entry = session.query(AuditLogModel).filter_by(action="update_user").first()
        assert entry is not None
        assert entry.details["updated_fields"]["email"] == "grace@x.com"


def test_update_nonexistent_user_returns_404(super_admin_client):
    """update_user() raises NotFoundError (-> 404) for a missing id, not a
    ValidationError -- the old 400 assumed a different exception type."""
    resp = super_admin_client.put("/api/v1/users/99999", json={"email": "x@y.com"})
    assert resp.status_code == 404


def test_update_no_hashed_password_in_response(super_admin_client):
    user = _seed("heidi")
    resp = super_admin_client.put(f"/api/v1/users/{user.id}", json={"email": "heidi@test.com"})
    assert "hashed_password" not in str(resp.json())


# ── DELETE /api/v1/users/{id} ─────────────────────────────────────────────────

def test_super_admin_can_deactivate_user(super_admin_client):
    user = _seed("ivan")
    resp = super_admin_client.delete(f"/api/v1/users/{user.id}", headers=elevated_headers("super-admin"))
    assert resp.status_code == 200
    assert resp.json()["data"]["is_active"] is False


def test_admin_can_deactivate_user_under_msp(admin_client):
    """MSP: Phase 4 — admin is system-admin under D24; deactivation is
    permitted at this privilege level. See
    test_admin_can_create_super_admin_under_msp for the full rationale."""
    user = _seed("judy")
    resp = admin_client.delete(f"/api/v1/users/{user.id}", headers=elevated_headers("admin"))
    assert resp.status_code == 200, resp.text


def test_deactivate_writes_audit_log(super_admin_client):
    user = _seed("karen")
    super_admin_client.delete(f"/api/v1/users/{user.id}", headers=elevated_headers("super-admin"))
    with get_session() as session:
        entry = session.query(AuditLogModel).filter_by(action="deactivate_user").first()
        assert entry is not None
        assert entry.resource_id == str(user.id)


def test_deactivate_last_system_admin_returns_422(super_admin_client):
    """ValidationError now maps to 422, not the old 400."""
    user = _seed("lastadmin", is_system_admin=True)
    resp = super_admin_client.delete(f"/api/v1/users/{user.id}", headers=elevated_headers("super-admin"))
    assert resp.status_code == 422


def test_operator_cannot_deactivate_user(operator_client):
    user = _seed("leo")
    resp = operator_client.delete(f"/api/v1/users/{user.id}")
    assert resp.status_code == 403
