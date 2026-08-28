"""MSP: Phase 3 T3.3 — POST/GET/DELETE /users/{id}/grants."""
from __future__ import annotations

import uuid

import pytest

from app.db.models import RoleAssignmentModel, SiteModel, UserModel
from app.db.session import get_session
from app.services import user_service
from app.schemas.user import UserCreate


@pytest.fixture()
def target_user_and_site():
    username = f"grant-target-{uuid.uuid4().hex[:6]}"
    user_service.create_user(UserCreate(username=username, password="p" * 12, role="observer"))
    with get_session() as session:
        uid = session.query(UserModel.id).filter_by(username=username).scalar()
        site = SiteModel(name=f"grants-site-{uuid.uuid4().hex[:6]}", kind="REGULAR")
        session.add(site)
        session.flush()
        sid = site.id
    yield uid, sid
    with get_session() as session:
        session.query(RoleAssignmentModel).filter_by(user_id=uid).delete(synchronize_session=False)
        session.query(UserModel).filter_by(id=uid).delete(synchronize_session=False)
        row = session.query(SiteModel).filter_by(id=sid).first()
        if row is not None:
            row.default_group_id = None
        session.flush()
        session.query(SiteModel).filter_by(id=sid).delete(synchronize_session=False)


def test_admin_can_grant_and_list_and_revoke(admin_client, target_user_and_site):
    uid, sid = target_user_and_site
    # Grant
    r = admin_client.post(
        f"/api/v1/users/{uid}/grants",
        json={"site_id": sid, "device_group_id": None, "role": "operator"},
    )
    assert r.status_code == 201, r.text
    grant_id = r.json()["data"]["id"]
    # List
    r = admin_client.get(f"/api/v1/users/{uid}/grants")
    assert r.status_code == 200
    listing = r.json()["data"]
    assert any(g["id"] == grant_id and g["role"] == "operator" for g in listing)
    # Revoke
    r = admin_client.delete(f"/api/v1/users/{uid}/grants/{grant_id}")
    assert r.status_code == 204
    r = admin_client.get(f"/api/v1/users/{uid}/grants")
    assert all(g["id"] != grant_id for g in r.json()["data"])


def test_grant_invalid_role_returns_422(admin_client, target_user_and_site):
    uid, sid = target_user_and_site
    r = admin_client.post(
        f"/api/v1/users/{uid}/grants",
        json={"site_id": sid, "device_group_id": None, "role": "super-admin"},
    )
    # super-admin isn't a valid assignment role — schema rejects with 422.
    assert r.status_code == 422


def test_grant_missing_site_returns_404(admin_client, target_user_and_site):
    uid, _ = target_user_and_site
    r = admin_client.post(
        f"/api/v1/users/{uid}/grants",
        json={"site_id": 999_999, "device_group_id": None, "role": "observer"},
    )
    assert r.status_code == 404


def test_non_admin_cannot_grant(observer_client, target_user_and_site):
    uid, sid = target_user_and_site
    r = observer_client.post(
        f"/api/v1/users/{uid}/grants",
        json={"site_id": sid, "device_group_id": None, "role": "observer"},
    )
    assert r.status_code == 403
