"""VisibilityScope — the caller's authorization footprint, resolved once
per request from RoleAssignmentRepository.scope_de() and reused for every
in-memory rol_para() check downstream.

Replaces the effective_role(session, user, ...) call that used to run a
fresh query per authorization check — the same caller iterating over N
devices no longer fires N queries.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VisibilityScope:
    es_system_admin: bool
    grants: tuple[tuple[int, "int | None", str], ...]  # (site_id, device_group_id|None, role)

    def rol_para(self, site_id: int, device_group_id: "int | None" = None) -> "str | None":
        """Most-specific match wins: a (site, group)-scoped grant shadows a
        (site, NULL) site-wide grant when the resource lies inside that
        group. Grants do not stack. Mirrors the policy that
        effective_role() implemented via SQL.
        """
        if self.es_system_admin:
            return "super-admin"
        if device_group_id is not None:
            for sid, gid, role in self.grants:
                if sid == site_id and gid == device_group_id:
                    return role
        for sid, gid, role in self.grants:
            if sid == site_id and gid is None:
                return role
        return None

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
