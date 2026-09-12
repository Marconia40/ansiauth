"""VisibilityScope — the caller's authorization footprint, resolved once
per request from RoleAssignmentRepository.scope_de() and reused for every
in-memory rol_para() check downstream.

One query per request produces the grant tuple; every downstream
authorization check runs in memory against that tuple.
"""
from __future__ import annotations

from dataclasses import dataclass


_ROLE_LEVEL = {"observer": 1, "operator": 2, "admin": 3}


@dataclass(frozen=True)
class VisibilityScope:
    es_system_admin: bool
    grants: tuple[tuple[int, "int | None", str], ...]  # (site_id, device_group_id|None, role)

    def rol_para(self, site_id: int, device_group_id: "int | None" = None) -> "str | None":
        """Max-role (additive): the effective role at a scope is the maximum
        of every grant that applies to it. Grants only elevate — a lower
        role on a group inside a site the user already admins has no
        effect on that group.

        Applicability rules:
          * A site-wide grant ``(site, NULL, role)`` applies to the whole
            site (both the site itself and every group within it).
          * A group-specific grant ``(site, group, role)`` applies only
            when ``device_group_id`` matches.

        Special case — ``device_group_id=None`` returns the site-wide
        grant only (a group-scoped admin does NOT count). Delegation
        authorization (D25) relies on this: a group-admin cannot delegate,
        only a site-wide admin can. Callers that want the effective role
        on a specific group must pass that group's id explicitly.
        """
        if self.es_system_admin:
            return "super-admin"
        site_wide: "str | None" = None
        group_specific: "str | None" = None
        for sid, gid, role in self.grants:
            if sid != site_id:
                continue
            if gid is None:
                site_wide = role
            elif device_group_id is not None and gid == device_group_id:
                group_specific = role
        if device_group_id is None:
            return site_wide
        candidates = [r for r in (site_wide, group_specific) if r is not None]
        if not candidates:
            return None
        return max(candidates, key=_ROLE_LEVEL.__getitem__)

    @property
    def site_ids(self) -> "set[int] | None":
        """Sites with a SITE-WIDE grant (gid is None). ``None`` means
        system-admin (no restriction).

        NOT the same as "every site where I have any grant" — a caller with
        only a device-group-scoped grant is intentionally excluded here. If
        you need that broader set (e.g. site_service.list_sites_for_user
        does), derive it directly from ``self.grants``:
        ``{sid for sid, _gid, _role in scope.grants}``.
        """
        return None if self.es_system_admin else {sid for sid, gid, _ in self.grants if gid is None}

    @property
    def device_group_ids(self) -> "set[int]":
        return set() if self.es_system_admin else {gid for _, gid, _ in self.grants if gid is not None}
