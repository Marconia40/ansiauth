"""MSP: Phase 3 T3.4 — device move endpoint tests.

Covers:
  * Same-site move (operator role suffices).
  * D8 clarification: ``device_group_id: null`` moves the device to its
    current site's Default group — the "remove from group" device-level action.
  * D_active_job: 409 when the device has a pending Job.
  * Cross-site move requires admin (asserted via role gating).
"""
from __future__ import annotations

import uuid

import pytest

from app.db.models import (
    DeviceGroupModel,
    DeviceModel,
    JobModel,
    RoleAssignmentModel,
    SiteModel,
    UserModel,
)
from app.db.session import get_session


@pytest.fixture()
def msp_move_scaffold():
    """Two regular sites, each with Default + one extra group, plus a device
    in Site-A's extra group. Bootstrap admin (system-admin) is used by
    ``admin_client``, so no per-test grants are needed for the happy path."""
    created = {"sites": [], "groups": [], "devices": [], "users": []}
    with get_session() as session:
        site_a = SiteModel(name=f"mv-A-{uuid.uuid4().hex[:6]}", kind="REGULAR")
        site_b = SiteModel(name=f"mv-B-{uuid.uuid4().hex[:6]}", kind="REGULAR")
        session.add_all([site_a, site_b])
        session.flush()
        default_a = DeviceGroupModel(name=f"mv-A-Default-{uuid.uuid4().hex[:6]}", site_id=site_a.id, is_default=True)
        default_b = DeviceGroupModel(name=f"mv-B-Default-{uuid.uuid4().hex[:6]}", site_id=site_b.id, is_default=True)
        extra_a = DeviceGroupModel(name=f"mv-A-Extra-{uuid.uuid4().hex[:6]}", site_id=site_a.id, is_default=False)
        session.add_all([default_a, default_b, extra_a])
        session.flush()
        site_a.default_group_id = default_a.id
        site_b.default_group_id = default_b.id
        dev = DeviceModel(
            name=f"mv-dev-{uuid.uuid4().hex[:6]}",
            host="10.10.10.10",
            vendor="cisco",
            platform="ios",
            username="u",
            encrypted_password="x",
            device_group_id=extra_a.id,
        )
        session.add(dev)
        session.flush()
        scaffold = {
            "site_a_id": site_a.id, "site_b_id": site_b.id,
            "default_a_id": default_a.id, "default_b_id": default_b.id,
            "extra_a_id": extra_a.id, "dev_name": dev.name,
        }
        created["sites"].extend([site_a.id, site_b.id])
        created["groups"].extend([default_a.id, default_b.id, extra_a.id])
        created["devices"].append(dev.name)

    yield scaffold

    with get_session() as session:
        session.query(JobModel).filter(JobModel.device.in_(created["devices"])).delete(
            synchronize_session=False
        )
        session.query(DeviceModel).filter(DeviceModel.name.in_(created["devices"])).delete(
            synchronize_session=False
        )
        for sid in created["sites"]:
            row = session.query(SiteModel).filter_by(id=sid).first()
            if row is not None:
                row.default_group_id = None
        session.flush()
        session.query(DeviceGroupModel).filter(DeviceGroupModel.id.in_(created["groups"])).delete(
            synchronize_session=False
        )
        session.query(SiteModel).filter(SiteModel.id.in_(created["sites"])).delete(
            synchronize_session=False
        )


def test_move_to_specific_group_same_site(admin_client, msp_move_scaffold):
    s = msp_move_scaffold
    r = admin_client.post(
        f"/api/v1/devices/{s['dev_name']}/move",
        json={"device_group_id": s["default_a_id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["device_group_id"] == s["default_a_id"]
    assert body["site_id"] == s["site_a_id"]


def test_move_with_null_group_id_resolves_to_site_default(admin_client, msp_move_scaffold):
    """D8 clarification: passing device_group_id=null moves the device to its
    current site's Default group (the device-level 'remove from group' action)."""
    s = msp_move_scaffold
    r = admin_client.post(
        f"/api/v1/devices/{s['dev_name']}/move",
        json={"device_group_id": None},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["device_group_id"] == s["default_a_id"]
    assert body["site_id"] == s["site_a_id"]


def test_move_no_op_returns_current_state(admin_client, msp_move_scaffold):
    s = msp_move_scaffold
    # Device is already in extra_a; moving to extra_a should succeed silently.
    r = admin_client.post(
        f"/api/v1/devices/{s['dev_name']}/move",
        json={"device_group_id": s["extra_a_id"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["device_group_id"] == s["extra_a_id"]


def test_move_rejected_when_pending_job(admin_client, msp_move_scaffold):
    """D_active_job: 409 while a Job on the same device is pending/running."""
    s = msp_move_scaffold
    with get_session() as session:
        session.add(JobModel(
            job_id=str(uuid.uuid4()),
            status="pending",
            device=s["dev_name"],
            playbook="stub.yml",
            created_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        ))
    r = admin_client.post(
        f"/api/v1/devices/{s['dev_name']}/move",
        json={"device_group_id": s["default_a_id"]},
    )
    assert r.status_code == 409, r.text


def test_move_to_missing_group_returns_400(admin_client, msp_move_scaffold):
    s = msp_move_scaffold
    r = admin_client.post(
        f"/api/v1/devices/{s['dev_name']}/move",
        json={"device_group_id": 99_999},
    )
    assert r.status_code == 400


def test_move_missing_device_returns_404(admin_client, msp_move_scaffold):
    r = admin_client.post(
        "/api/v1/devices/no-such-device/move",
        json={"device_group_id": msp_move_scaffold["default_a_id"]},
    )
    assert r.status_code == 404


def test_operator_can_do_same_site_move(msp_move_scaffold):
    """A user with an operator grant on the source device may do a same-site move."""
    from app.core.security import create_access_token
    from fastapi.testclient import TestClient
    from app.main import app

    s = msp_move_scaffold
    username = f"op-{uuid.uuid4().hex[:6]}"
    with get_session() as session:
        user = UserModel(
            username=username, hashed_password="x", is_system_admin=False,
        )
        session.add(user)
        session.flush()
        session.add(RoleAssignmentModel(
            user_id=user.id, site_id=s["site_a_id"], device_group_id=None, role="operator",
        ))
    token = create_access_token({"sub": username, "role": "operator"})
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    r = client.post(
        f"/api/v1/devices/{s['dev_name']}/move",
        json={"device_group_id": s["default_a_id"]},
    )
    assert r.status_code == 200, r.text


def test_operator_cross_site_move_forbidden(msp_move_scaffold):
    """Cross-site moves require admin on both sides — an operator can't cross."""
    from app.core.security import create_access_token
    from fastapi.testclient import TestClient
    from app.main import app

    s = msp_move_scaffold
    username = f"op-cross-{uuid.uuid4().hex[:6]}"
    with get_session() as session:
        user = UserModel(
            username=username, hashed_password="x", is_system_admin=False,
        )
        session.add(user)
        session.flush()
        # Operator on both sites but not admin — cross-site move should still fail.
        for sid in (s["site_a_id"], s["site_b_id"]):
            session.add(RoleAssignmentModel(
                user_id=user.id, site_id=sid, device_group_id=None, role="operator",
            ))
    token = create_access_token({"sub": username, "role": "operator"})
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    r = client.post(
        f"/api/v1/devices/{s['dev_name']}/move",
        json={"device_group_id": s["default_b_id"]},
    )
    assert r.status_code == 403, r.text
