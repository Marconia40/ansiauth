"""MSP: Phase 3 T3.1 — unit tests for services.effective_role.

Covers the most-specific-wins semantics documented in
MSP_IMPLEMENTATION_PLAN.md §10.5 and the phase-3 spec §5.
"""
from __future__ import annotations

import pytest

from app.db.models import (
    DeviceGroupModel,
    DeviceModel,
    RoleAssignmentModel,
    SiteModel,
    UserModel,
)
from app.db.session import get_session
from app.services.effective_role import effective_role


@pytest.fixture()
def isolated_msp_scaffold():
    """Fresh scaffold: two sites, one group per site, one device per group,
    one user with a single site-wide observer grant on the first site.

    Cleans up on teardown so tests remain independent.
    """
    created_user_ids: list[int] = []
    created_site_ids: list[int] = []

    with get_session() as session:
        site_a = SiteModel(name="msp-eff-A", kind="REGULAR")
        site_b = SiteModel(name="msp-eff-B", kind="REGULAR")
        session.add_all([site_a, site_b])
        session.flush()
        group_a = DeviceGroupModel(name="msp-eff-A-Default", site_id=site_a.id, is_default=True)
        group_b = DeviceGroupModel(name="msp-eff-B-Default", site_id=site_b.id, is_default=True)
        session.add_all([group_a, group_b])
        session.flush()
        site_a.default_group_id = group_a.id
        site_b.default_group_id = group_b.id
        dev_a = DeviceModel(
            name="msp-eff-dev-A",
            host="10.0.0.1",
            vendor="cisco",
            platform="ios",
            username="u",
            encrypted_password="x",
            device_group_id=group_a.id,
            site_id=site_a.id,
        )
        dev_b = DeviceModel(
            name="msp-eff-dev-B",
            host="10.0.0.2",
            vendor="cisco",
            platform="ios",
            username="u",
            encrypted_password="x",
            device_group_id=group_b.id,
            site_id=site_b.id,
        )
        session.add_all([dev_a, dev_b])
        user = UserModel(
            username="msp-eff-user",
            hashed_password="x",
            role="observer",
            is_system_admin=False,
        )
        session.add(user)
        session.flush()
        # Site-wide observer grant on site A only.
        session.add(RoleAssignmentModel(
            user_id=user.id, site_id=site_a.id, device_group_id=None, role="observer",
        ))
        session.flush()
        scaffold = {
            "user": {"id": user.id, "username": user.username, "is_system_admin": False},
            "site_a_id": site_a.id, "site_b_id": site_b.id,
            "group_a_id": group_a.id, "group_b_id": group_b.id,
            "dev_a_name": dev_a.name, "dev_b_name": dev_b.name,
        }
        created_user_ids.append(user.id)
        created_site_ids.extend([site_a.id, site_b.id])

    yield scaffold

    # Teardown — cascades take care of grants + groups + devices.
    with get_session() as session:
        session.query(RoleAssignmentModel).filter(
            RoleAssignmentModel.user_id.in_(created_user_ids)
        ).delete(synchronize_session=False)
        session.query(DeviceModel).filter(
            DeviceModel.name.in_(["msp-eff-dev-A", "msp-eff-dev-B"])
        ).delete(synchronize_session=False)
        session.query(DeviceGroupModel).filter(
            DeviceGroupModel.id.in_([]) | (DeviceGroupModel.site_id.in_(created_site_ids))
        ).delete(synchronize_session=False)
        # Break cyclic FK before deleting the site.
        for sid in created_site_ids:
            row = session.query(SiteModel).filter_by(id=sid).first()
            if row is not None:
                row.default_group_id = None
        session.flush()
        session.query(SiteModel).filter(SiteModel.id.in_(created_site_ids)).delete(
            synchronize_session=False
        )
        session.query(UserModel).filter(UserModel.id.in_(created_user_ids)).delete(
            synchronize_session=False
        )


def test_system_admin_returns_super_admin(isolated_msp_scaffold):
    scaffold = isolated_msp_scaffold
    admin_user = {"id": scaffold["user"]["id"], "is_system_admin": True}
    with get_session() as session:
        assert effective_role(session, admin_user, "site", scaffold["site_b_id"]) == "super-admin"
        assert effective_role(session, admin_user, "device", scaffold["dev_b_name"]) == "super-admin"


def test_site_wide_grant_covers_group_and_device(isolated_msp_scaffold):
    scaffold = isolated_msp_scaffold
    user = scaffold["user"]
    with get_session() as session:
        assert effective_role(session, user, "site", scaffold["site_a_id"]) == "observer"
        assert effective_role(session, user, "device_group", scaffold["group_a_id"]) == "observer"
        assert effective_role(session, user, "device", scaffold["dev_a_name"]) == "observer"


def test_no_grant_on_other_site_returns_none(isolated_msp_scaffold):
    scaffold = isolated_msp_scaffold
    user = scaffold["user"]
    with get_session() as session:
        assert effective_role(session, user, "site", scaffold["site_b_id"]) is None
        assert effective_role(session, user, "device_group", scaffold["group_b_id"]) is None
        assert effective_role(session, user, "device", scaffold["dev_b_name"]) is None


def test_group_scoped_grant_wins_over_site_wide(isolated_msp_scaffold):
    """Most-specific-wins: a group-scoped operator grant beats the site-wide
    observer grant when accessing that specific group / its devices."""
    scaffold = isolated_msp_scaffold
    user = scaffold["user"]
    with get_session() as session:
        session.add(RoleAssignmentModel(
            user_id=user["id"],
            site_id=scaffold["site_a_id"],
            device_group_id=scaffold["group_a_id"],
            role="operator",
        ))
    with get_session() as session:
        # Group-scoped operator wins on the group and its device …
        assert effective_role(session, user, "device_group", scaffold["group_a_id"]) == "operator"
        assert effective_role(session, user, "device", scaffold["dev_a_name"]) == "operator"
        # … but the site-wide observer grant still applies to the site itself.
        assert effective_role(session, user, "site", scaffold["site_a_id"]) == "observer"


def test_unknown_resource_returns_none(isolated_msp_scaffold):
    user = isolated_msp_scaffold["user"]
    with get_session() as session:
        assert effective_role(session, user, "site", 999_999) is None
        assert effective_role(session, user, "device", "no-such-device") is None


def test_invalid_resource_type_raises():
    with get_session() as session:
        with pytest.raises(ValueError):
            effective_role(session, {"id": 1, "is_system_admin": False}, "bogus", 1)
