"""MSP: Phase 4 T4.2 — the legacy device_group_members endpoints still
work and now carry ``Deprecation: true`` / ``Sunset: Phase-5`` headers.

Removed in Phase 5. Kept here so operators using the pre-cutover API
surface still succeed while their scripts get updated to the new
``POST /devices/{name}/move`` shape.
"""
from __future__ import annotations

import uuid

import pytest

from app.db.models import (
    DeviceGroupModel,
    DeviceModel,
    SiteModel,
)
from app.db.session import get_session


@pytest.fixture()
def deprecated_scaffold(admin_client):
    """Seeds one REGULAR site with two groups (Default + extra), plus a
    device in the extra group. Bootstrap admin (system-admin) drives all
    requests so authz never blocks."""
    tag = uuid.uuid4().hex[:6]
    site_name = f"dep-site-{tag}"
    device_name = f"dep-dev-{tag}"

    site_id = admin_client.post(
        "/api/v1/sites/", json={"name": site_name},
    ).json()["data"]["id"]
    with get_session() as session:
        default_group_id = session.query(SiteModel.default_group_id).filter_by(
            id=site_id,
        ).scalar()
    extra_group_id = admin_client.post(
        "/api/v1/device-groups/",
        json={"name": f"Extra-{tag}", "site_id": site_id, "description": ""},
    ).json()["data"]["id"]

    admin_client.post(
        "/api/v1/devices/",
        json={
            "name": device_name, "host": "10.0.0.9", "vendor": "cisco",
            "platform": "ios", "username": "u", "password": "pw",
            "site_id": site_id, "device_group_id": extra_group_id,
        },
    )

    scaffold = {
        "site_id": site_id,
        "default_group_id": default_group_id,
        "extra_group_id": extra_group_id,
        "device_name": device_name,
    }

    yield scaffold

    # Teardown — delete the device, groups, and site in FK-safe order.
    with get_session() as session:
        session.query(DeviceModel).filter_by(name=device_name).delete(
            synchronize_session=False,
        )
        row = session.query(SiteModel).filter_by(id=site_id).first()
        if row is not None:
            row.default_group_id = None
        session.flush()
        session.query(DeviceGroupModel).filter_by(site_id=site_id).delete(
            synchronize_session=False,
        )
        session.query(SiteModel).filter_by(id=site_id).delete(
            synchronize_session=False,
        )


def test_add_member_still_works_but_flagged_deprecated(admin_client, deprecated_scaffold):
    s = deprecated_scaffold
    r = admin_client.post(
        f"/api/v1/device-groups/{s['default_group_id']}/members",
        json={"device_name": s["device_name"]},
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("Deprecation") == "true"
    assert r.headers.get("Sunset") == "Phase-5"
    # Successor pointer is optional per RFC 8288 but we set it — confirm.
    assert "successor-version" in (r.headers.get("Link") or "")
    # Device's authoritative FK now points at the default group.
    with get_session() as session:
        gid = session.query(DeviceModel.device_group_id).filter_by(
            name=s["device_name"],
        ).scalar()
        assert gid == s["default_group_id"]


def test_remove_member_still_works_but_flagged_deprecated(admin_client, deprecated_scaffold):
    s = deprecated_scaffold
    # Device starts in extra_group; removing it should land it in Default
    # via the D8 device-level semantics.
    r = admin_client.delete(
        f"/api/v1/device-groups/{s['extra_group_id']}/members/{s['device_name']}",
    )
    assert r.status_code == 200, r.text
    assert r.headers.get("Deprecation") == "true"
    assert r.headers.get("Sunset") == "Phase-5"
    with get_session() as session:
        gid = session.query(DeviceModel.device_group_id).filter_by(
            name=s["device_name"],
        ).scalar()
        # D8: device landed in the site's Default group.
        assert gid == s["default_group_id"]
