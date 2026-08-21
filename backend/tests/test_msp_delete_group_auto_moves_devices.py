"""MSP: Phase 3 T3.6 — D19: deleting a group auto-moves its members to the
Site's Default group before removing the group.
"""
from __future__ import annotations

import uuid

import pytest

from app.db.models import DeviceGroupModel, DeviceModel, SiteModel
from app.db.session import get_session


@pytest.fixture()
def d19_scaffold():
    with get_session() as session:
        site = SiteModel(name=f"d19-site-{uuid.uuid4().hex[:6]}", kind="REGULAR")
        session.add(site)
        session.flush()
        default_group = DeviceGroupModel(name=f"d19-default-{uuid.uuid4().hex[:6]}", site_id=site.id, is_default=True)
        extra_group = DeviceGroupModel(name=f"d19-extra-{uuid.uuid4().hex[:6]}", site_id=site.id, is_default=False)
        session.add_all([default_group, extra_group])
        session.flush()
        site.default_group_id = default_group.id
        devices = []
        for i in range(3):
            dev = DeviceModel(
                name=f"d19-dev-{uuid.uuid4().hex[:6]}-{i}",
                host=f"10.0.99.{i}",
                vendor="cisco",
                platform="ios",
                username="u",
                encrypted_password="x",
                device_group_id=extra_group.id,
                site_id=site.id,
            )
            session.add(dev)
            devices.append(dev.name)
        session.flush()
        scaffold = {
            "site_id": site.id,
            "default_id": default_group.id,
            "extra_id": extra_group.id,
            "device_names": devices,
        }

    yield scaffold

    with get_session() as session:
        session.query(DeviceModel).filter(
            DeviceModel.name.in_(scaffold["device_names"])
        ).delete(synchronize_session=False)
        row = session.query(SiteModel).filter_by(id=scaffold["site_id"]).first()
        if row is not None:
            row.default_group_id = None
        session.flush()
        session.query(DeviceGroupModel).filter(DeviceGroupModel.site_id == scaffold["site_id"]).delete(
            synchronize_session=False
        )
        session.query(SiteModel).filter_by(id=scaffold["site_id"]).delete(
            synchronize_session=False
        )


def test_delete_group_auto_moves_devices_to_default(admin_client, d19_scaffold):
    s = d19_scaffold
    r = admin_client.delete(f"/api/v1/device-groups/{s['extra_id']}")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["deleted_group_id"] == s["extra_id"]
    assert sorted(data["moved_devices"]) == sorted(s["device_names"])
    # Every device now belongs to the site's Default group.
    with get_session() as session:
        for name in s["device_names"]:
            row = session.query(DeviceModel.device_group_id).filter_by(name=name).first()
            assert row[0] == s["default_id"], f"{name} should have moved to Default"


def test_delete_default_group_rejected_by_d7(admin_client, d19_scaffold):
    s = d19_scaffold
    r = admin_client.delete(f"/api/v1/device-groups/{s['default_id']}")
    assert r.status_code in (400, 422), r.text
    assert "default" in r.text.lower() or "immutable" in r.text.lower()
