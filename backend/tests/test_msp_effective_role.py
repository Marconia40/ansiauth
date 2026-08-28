"""MSP: Phase 3 T3.1 — unit tests for services.effective_role.

Covers the most-specific-wins semantics documented in
MSP_IMPLEMENTATION_PLAN.md §10.5 and the phase-3 spec §5.
"""
from __future__ import annotations

import uuid

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


VALID_RESOURCE_TYPES = frozenset({"site", "device_group", "device"})


@pytest.fixture()
def isolated_msp_scaffold():
    """Fresh scaffold: two sites, one group per site, one device per group,
    one user with a single site-wide observer grant on the first site.

    Uses uuid-suffixed names so re-runs never collide, and tears down in
    FK-safe order under SQLite FK enforcement (Phase 4).
    """
    tag = uuid.uuid4().hex[:6]
    site_a_name = f"msp-eff-A-{tag}"
    site_b_name = f"msp-eff-B-{tag}"
    dev_a_name = f"msp-eff-dev-A-{tag}"
    dev_b_name = f"msp-eff-dev-B-{tag}"
    user_name = f"msp-eff-user-{tag}"

    with get_session() as session:
        site_a = SiteModel(name=site_a_name, kind="REGULAR")
        site_b = SiteModel(name=site_b_name, kind="REGULAR")
        session.add_all([site_a, site_b])
        session.flush()
        group_a = DeviceGroupModel(name=f"Default-{tag}-A", site_id=site_a.id, is_default=True)
        group_b = DeviceGroupModel(name=f"Default-{tag}-B", site_id=site_b.id, is_default=True)
        session.add_all([group_a, group_b])
        session.flush()
        site_a.default_group_id = group_a.id
        site_b.default_group_id = group_b.id
        dev_a = DeviceModel(
            name=dev_a_name, host="10.0.0.1", vendor="cisco", platform="ios",
            username="u", encrypted_password="x",
            device_group_id=group_a.id,
        )
        dev_b = DeviceModel(
            name=dev_b_name, host="10.0.0.2", vendor="cisco", platform="ios",
            username="u", encrypted_password="x",
            device_group_id=group_b.id,
        )
        session.add_all([dev_a, dev_b])
        user = UserModel(
            username=user_name, hashed_password="x",
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
            "user_id": user.id,
            "site_a_id": site_a.id, "site_b_id": site_b.id,
            "group_a_id": group_a.id, "group_b_id": group_b.id,
            "dev_a_name": dev_a_name, "dev_b_name": dev_b_name,
            "site_ids": [site_a.id, site_b.id],
            "device_names": [dev_a_name, dev_b_name],
        }

    yield scaffold

    # Teardown — FK-safe order under SQLite FK enforcement.
    with get_session() as session:
        session.query(RoleAssignmentModel).filter_by(user_id=scaffold["user_id"]).delete(
            synchronize_session=False
        )
        session.query(DeviceModel).filter(
            DeviceModel.name.in_(scaffold["device_names"])
        ).delete(synchronize_session=False)
        # Null the sites' default_group_id back-ref before dropping groups
        # (RESTRICT FK on sites.default_group_id → device_groups.id).
        for sid in scaffold["site_ids"]:
            row = session.query(SiteModel).filter_by(id=sid).first()
            if row is not None:
                row.default_group_id = None
        session.flush()
        session.query(DeviceGroupModel).filter(
            DeviceGroupModel.site_id.in_(scaffold["site_ids"])
        ).delete(synchronize_session=False)
        session.query(SiteModel).filter(
            SiteModel.id.in_(scaffold["site_ids"])
        ).delete(synchronize_session=False)
        session.query(UserModel).filter_by(id=scaffold["user_id"]).delete(
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
