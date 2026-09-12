"""Unit tests for ``VisibilityScope.rol_para()`` — max-role semantics.

These tests exercise the pure in-memory resolution on hand-constructed
grant tuples, without going through the DB or the repository. Paired
with the DB-integration coverage in ``test_msp_effective_role.py``.

See ``docs/USER_PERMISSIONS_UX_REDESIGN.md`` §2.1 for the semantic
change (from most-specific-wins to additive/max-role).
"""
from __future__ import annotations

from app.models.visibility_scope import VisibilityScope


SITE_A, SITE_B = 1, 2
GROUP_A1, GROUP_A2, GROUP_B1 = 10, 11, 20


def _scope(*grants):
    return VisibilityScope(es_system_admin=False, grants=tuple(grants))


def test_system_admin_returns_super_admin_for_any_scope():
    scope = VisibilityScope(es_system_admin=True, grants=())
    assert scope.rol_para(SITE_A, None) == "super-admin"
    assert scope.rol_para(SITE_A, GROUP_A1) == "super-admin"
    assert scope.rol_para(999, 999) == "super-admin"


def test_no_grants_returns_none():
    scope = _scope()
    assert scope.rol_para(SITE_A, None) is None
    assert scope.rol_para(SITE_A, GROUP_A1) is None


def test_site_wide_grant_applies_to_site_and_all_groups():
    scope = _scope((SITE_A, None, "observer"))
    assert scope.rol_para(SITE_A, None) == "observer"
    assert scope.rol_para(SITE_A, GROUP_A1) == "observer"
    assert scope.rol_para(SITE_A, GROUP_A2) == "observer"


def test_grant_on_other_site_is_ignored():
    scope = _scope((SITE_A, None, "admin"))
    assert scope.rol_para(SITE_B, None) is None
    assert scope.rol_para(SITE_B, GROUP_B1) is None


def test_group_grant_elevates_above_site_wide():
    # Site-wide observer + group-scoped operator on GROUP_A1.
    scope = _scope(
        (SITE_A, None, "observer"),
        (SITE_A, GROUP_A1, "operator"),
    )
    assert scope.rol_para(SITE_A, GROUP_A1) == "operator"
    # Other groups on the same site fall back to the site-wide grant.
    assert scope.rol_para(SITE_A, GROUP_A2) == "observer"
    # Site-wide lookup returns the site-wide grant only.
    assert scope.rol_para(SITE_A, None) == "observer"


def test_group_grant_below_site_wide_does_not_downgrade():
    # This is the crux of the semantic change: an observer grant on a
    # group inside a site the user already admins has NO effect.
    scope = _scope(
        (SITE_A, None, "admin"),
        (SITE_A, GROUP_A1, "observer"),
    )
    assert scope.rol_para(SITE_A, GROUP_A1) == "admin"
    assert scope.rol_para(SITE_A, GROUP_A2) == "admin"
    assert scope.rol_para(SITE_A, None) == "admin"


def test_group_only_admin_is_not_site_admin():
    # ``rol_para(site, None)`` returns the site-wide grant only —
    # a group-scoped admin must NOT be treated as a site-admin
    # (D25 relies on this for delegation authorization).
    scope = _scope((SITE_A, GROUP_A1, "admin"))
    assert scope.rol_para(SITE_A, None) is None
    assert scope.rol_para(SITE_A, GROUP_A1) == "admin"
    # Other groups on the same site have no matching grant.
    assert scope.rol_para(SITE_A, GROUP_A2) is None


def test_multiple_group_grants_resolve_independently():
    scope = _scope(
        (SITE_A, None, "observer"),
        (SITE_A, GROUP_A1, "admin"),
        (SITE_A, GROUP_A2, "operator"),
    )
    assert scope.rol_para(SITE_A, GROUP_A1) == "admin"
    assert scope.rol_para(SITE_A, GROUP_A2) == "operator"
    assert scope.rol_para(SITE_A, None) == "observer"


def test_grants_on_different_sites_do_not_leak():
    scope = _scope(
        (SITE_A, None, "admin"),
        (SITE_B, None, "observer"),
        (SITE_B, GROUP_B1, "operator"),
    )
    assert scope.rol_para(SITE_A, None) == "admin"
    assert scope.rol_para(SITE_A, GROUP_A1) == "admin"
    assert scope.rol_para(SITE_B, None) == "observer"
    assert scope.rol_para(SITE_B, GROUP_B1) == "operator"
