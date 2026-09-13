"""MSP: Phase 4 T4.3 — D27 audit scoping.

Verifies ``GET /audit`` filters rows through role_assignments for non
system-admin callers. System-admins bypass; site-admins see only rows for
devices/groups/sites their grants cover, plus device-unrelated auth events.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.db.models import (
    AuditLogModel,
    RoleAssignmentModel,
    SiteModel,
    UserModel,
)
from app.db.session import get_session
from app.main import app


@pytest.fixture()
def audit_scaffold():
    """Seeds:
      * two REGULAR sites (A and B)
      * an observer user with a site-wide observer grant on site A ONLY
      * one audit_logs row per site (via raw INSERT, since we're testing
        the read path, not the write path)
    """
    tag = uuid.uuid4().hex[:6]
    site_a_name = f"aud-A-{tag}"
    site_b_name = f"aud-B-{tag}"
    user_name = f"aud-user-{tag}"

    site_a_id = site_b_id = user_id = None
    with get_session() as session:
        for name in (site_a_name, site_b_name):
            s = SiteModel(name=name, kind="REGULAR")
            session.add(s)
            session.flush()
            if name == site_a_name:
                site_a_id = s.id
            else:
                site_b_id = s.id
        user = UserModel(
            username=user_name, hashed_password="x",
            is_system_admin=False,
        )
        session.add(user)
        session.flush()
        user_id = user.id
        session.add(RoleAssignmentModel(
            user_id=user.id, site_id=site_a_id,
            device_group_id=None, role="observer",
        ))
        # Site-scoped audit rows (device is None → resource='site' is the
        # scoping key).
        import datetime as _dt
        now = _dt.datetime.now(_dt.timezone.utc)
        for sid in (site_a_id, site_b_id):
            session.add(AuditLogModel(
                timestamp=now, user="admin", action="update_site",
                resource="site", resource_id=str(sid),
                status="success", details={"site_id": sid},
            ))
        # A device-unrelated auth row that every caller should see.
        session.add(AuditLogModel(
            timestamp=now, user=user_name, action="login",
            resource="auth", resource_id=None,
            status="success", details={"username": user_name},
        ))

    token = create_access_token({"sub": user_name, "role": "observer"})
    observer_client = TestClient(app)
    observer_client.headers.update({"Authorization": f"Bearer {token}"})

    scaffold = {
        "site_a_id": site_a_id, "site_b_id": site_b_id,
        "user_id": user_id, "user_name": user_name,
        "observer_client": observer_client,
    }
    yield scaffold

    # Teardown
    with get_session() as session:
        session.query(AuditLogModel).filter(
            AuditLogModel.resource_id.in_([str(site_a_id), str(site_b_id)])
        ).delete(synchronize_session=False)
        session.query(AuditLogModel).filter(
            AuditLogModel.user == user_name,
        ).delete(synchronize_session=False)
        session.query(RoleAssignmentModel).filter_by(user_id=user_id).delete(
            synchronize_session=False,
        )
        session.query(UserModel).filter_by(id=user_id).delete(synchronize_session=False)
        session.query(SiteModel).filter(
            SiteModel.id.in_([site_a_id, site_b_id])
        ).delete(synchronize_session=False)


def test_system_admin_sees_all_audit_rows(admin_client, audit_scaffold):
    """Bootstrap admin is system-admin under Phase 4 → sees every row."""
    s = audit_scaffold
    r = admin_client.get("/api/v1/audit/")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]["items"]
    ids_seen = {row["resource_id"] for row in rows if row["resource"] == "site"}
    assert str(s["site_a_id"]) in ids_seen
    assert str(s["site_b_id"]) in ids_seen


def test_observer_sees_only_scoped_rows(audit_scaffold):
    """Observer with a grant only on site A must NOT see site B's audit row."""
    s = audit_scaffold
    r = s["observer_client"].get("/api/v1/audit/")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]["items"]
    site_ids_seen = {row["resource_id"] for row in rows if row["resource"] == "site"}
    assert str(s["site_a_id"]) in site_ids_seen, (
        f"Expected site A's audit row; got {site_ids_seen}"
    )
    assert str(s["site_b_id"]) not in site_ids_seen, (
        f"Site B is outside the observer's scope; got {site_ids_seen}"
    )


def test_observer_still_sees_own_auth_events(audit_scaffold):
    """Device-unrelated events (login/refresh/logout) are always visible so
    users can audit their own session activity."""
    s = audit_scaffold
    r = s["observer_client"].get("/api/v1/audit/")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]["items"]
    assert any(
        row["resource"] == "auth" and row["user"] == s["user_name"]
        for row in rows
    ), "observer should see their own login row"
