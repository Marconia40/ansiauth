"""Effective-role resolution via ``VisibilityScope.rol_para()``.

Covers the max-role (additive) semantics documented in
``docs/USER_PERMISSIONS_UX_REDESIGN.md`` §2.1: a grant may only elevate
the effective role at a scope, never downgrade it. Replaces the earlier
most-specific-wins tests that pointed at a removed ``effective_role``
service module.
"""
from __future__ import annotations

import uuid

import pytest

from app.composition import role_assignment_repository
from app.db.models import (
    DeviceGroupModel,
    DeviceModel,
    RoleAssignmentModel,
    SiteModel,
    UserModel,
)
from app.db.session import get_session


def _scope_for(user_row: dict):
    return role_assignment_repository.scope_de(user_row)


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


def test_system_admin_bypasses_grants(isolated_msp_scaffold):
    scaffold = isolated_msp_scaffold
    admin_user = {"id": scaffold["user"]["id"], "is_system_admin": True}
    scope = _scope_for(admin_user)
    # System-admins are super-admin everywhere, including sites they hold
    # no grants on.
    assert scope.rol_para(scaffold["site_b_id"], None) == "super-admin"
    assert scope.rol_para(scaffold["site_b_id"], scaffold["group_b_id"]) == "super-admin"


def test_site_wide_grant_covers_site_and_its_groups(isolated_msp_scaffold):
    scaffold = isolated_msp_scaffold
    scope = _scope_for(scaffold["user"])
    assert scope.rol_para(scaffold["site_a_id"], None) == "observer"
    assert scope.rol_para(scaffold["site_a_id"], scaffold["group_a_id"]) == "observer"


def test_no_grant_on_other_site_returns_none(isolated_msp_scaffold):
    scaffold = isolated_msp_scaffold
    scope = _scope_for(scaffold["user"])
    assert scope.rol_para(scaffold["site_b_id"], None) is None
    assert scope.rol_para(scaffold["site_b_id"], scaffold["group_b_id"]) is None


def test_group_grant_can_elevate_above_site_wide(isolated_msp_scaffold):
    """Additive: a group-scoped operator grant elevates the effective role
    on that group above the site-wide observer grant."""
    scaffold = isolated_msp_scaffold
    user = scaffold["user"]
    with get_session() as session:
        session.add(RoleAssignmentModel(
            user_id=user["id"],
            site_id=scaffold["site_a_id"],
            device_group_id=scaffold["group_a_id"],
            role="operator",
        ))
    scope = _scope_for(user)
    # Group-scoped operator elevates the group above the site-wide observer.
    assert scope.rol_para(scaffold["site_a_id"], scaffold["group_a_id"]) == "operator"
    # The site itself still resolves to the site-wide grant only.
    assert scope.rol_para(scaffold["site_a_id"], None) == "observer"


def test_group_grant_below_site_wide_does_not_downgrade(isolated_msp_scaffold):
    """Max-role rule (the crux of the semantic change): if the user is
    admin site-wide, a lower group-specific role has NO effect on that
    group — the effective role stays admin."""
    scaffold = isolated_msp_scaffold
    user = scaffold["user"]
    # Promote the site-wide grant from observer to admin, then add a
    # lower-role group-scoped grant on the same site.
    with get_session() as session:
        session.query(RoleAssignmentModel).filter_by(
            user_id=user["id"],
            site_id=scaffold["site_a_id"],
            device_group_id=None,
        ).update({"role": "admin"})
        session.add(RoleAssignmentModel(
            user_id=user["id"],
            site_id=scaffold["site_a_id"],
            device_group_id=scaffold["group_a_id"],
            role="observer",
        ))
    scope = _scope_for(user)
    # The group-scoped observer grant cannot downgrade the site-wide admin.
    assert scope.rol_para(scaffold["site_a_id"], scaffold["group_a_id"]) == "admin"
    assert scope.rol_para(scaffold["site_a_id"], None) == "admin"


def test_group_only_admin_does_not_grant_site_wide(isolated_msp_scaffold):
    """``rol_para(site_id, None)`` returns the site-wide grant only —
    D25 relies on this: a group-scoped admin must NOT be treated as a
    site-admin for delegation purposes."""
    scaffold = isolated_msp_scaffold
    user = scaffold["user"]
    # Remove the site-wide observer grant, add a group-scoped admin.
    with get_session() as session:
        session.query(RoleAssignmentModel).filter_by(
            user_id=user["id"],
            site_id=scaffold["site_a_id"],
            device_group_id=None,
        ).delete(synchronize_session=False)
        session.add(RoleAssignmentModel(
            user_id=user["id"],
            site_id=scaffold["site_a_id"],
            device_group_id=scaffold["group_a_id"],
            role="admin",
        ))
    scope = _scope_for(user)
    # Site-wide lookup returns None — the user is not a site-admin.
    assert scope.rol_para(scaffold["site_a_id"], None) is None
    # But the group lookup finds the group-scoped admin.
    assert scope.rol_para(scaffold["site_a_id"], scaffold["group_a_id"]) == "admin"
    # Other groups on the same site have no matching grant.
    assert scope.rol_para(scaffold["site_a_id"], 999_999) is None
