"""MSP: Phase 3 T3.8 — D14: the Base-Infrastructure Site is invisible to
non system-admins even under the MSP-strict flag.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.security import create_access_token
from app.db.models import RoleAssignmentModel, SiteModel, UserModel
from app.db.session import get_session
from app.repositories.site_repository import BASE_INFRA_SITE_KIND
from fastapi.testclient import TestClient

# site_service.py was deleted by the migration to FINAL_ARCHITECTURE.md --
# SiteRepository.crear_con_grupo_default(kind=BASE_INFRA_SITE_KIND) is the
# idempotent bootstrap replacement (see app/main.py's own bootstrap call).


def _ensure_base_infrastructure():
    from app.composition import site_repository
    return site_repository.crear_con_grupo_default(
        "Base Infrastructure", "System-managed base infrastructure site.",
        kind=BASE_INFRA_SITE_KIND,
    )


@pytest.fixture()
def flag_on():
    """Legacy no-op fixture — MSP strict-hierarchy is unconditional post-Phase-5.
    Retained so callers that request the fixture still resolve."""
    yield


@pytest.fixture()
def observer_scoped_client():
    """Observer with a grant only on the Base-Infra site — should still not
    see it (D14). Bootstrap ensures Base-Infra exists."""
    _ensure_base_infrastructure()
    username = f"d14-obs-{uuid.uuid4().hex[:6]}"
    with get_session() as session:
        user = UserModel(username=username, hashed_password="x", is_system_admin=False)
        session.add(user)
        session.flush()
        user_id = user.id
        base_site_id = session.query(SiteModel.id).filter_by(kind=BASE_INFRA_SITE_KIND).scalar()
        session.add(RoleAssignmentModel(
            user_id=user_id, site_id=base_site_id, device_group_id=None, role="observer",
        ))
    token = create_access_token({"sub": username, "role": "observer"})
    from app.main import app
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    yield client, base_site_id
    with get_session() as session:
        session.query(RoleAssignmentModel).filter_by(user_id=user_id).delete(
            synchronize_session=False
        )
        session.query(UserModel).filter_by(id=user_id).delete(
            synchronize_session=False
        )


def test_base_infra_hidden_from_non_system_admin_list(flag_on, observer_scoped_client):
    client, base_site_id = observer_scoped_client
    r = client.get("/api/v1/sites/")
    assert r.status_code == 200
    ids = {s["id"] for s in r.json()["data"]}
    assert base_site_id not in ids, "Base-Infrastructure must not appear for non system-admins (D14)"


def test_base_infra_get_returns_404_to_non_system_admin(flag_on, observer_scoped_client):
    client, base_site_id = observer_scoped_client
    r = client.get(f"/api/v1/sites/{base_site_id}")
    assert r.status_code == 404, r.text


def test_base_infra_cannot_be_deleted(admin_client):
    _ensure_base_infrastructure()
    with get_session() as session:
        base_site_id = session.query(SiteModel.id).filter_by(kind=BASE_INFRA_SITE_KIND).scalar()
    r = admin_client.delete(f"/api/v1/sites/{base_site_id}")
    # 400 (rejected as a system-managed site), 403 under MSP flag if admin
    # isn't system-admin, or 422 depending on ValidationError shape.
    assert r.status_code in (400, 403, 422), r.text
