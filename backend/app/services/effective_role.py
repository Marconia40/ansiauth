"""MSP: Phase 3 — resolve a caller's effective role on a given resource.

Pure read-only resolver. No writes, no exceptions on "denied" — callers
(``core/scope.require_scope`` and ``services/inventory_service``) turn the
``None`` return into an HTTP 403 / 404 as appropriate.

Policy (see MSP_IMPLEMENTATION_PLAN.md §10.5, decision D11):
  1. ``is_system_admin`` → ``super-admin`` unconditionally.
  2. Most-specific match wins: a ``(site, device_group)``-scoped grant
     shadows a ``(site, NULL)`` site-wide grant when the resource lies inside
     that group.
  3. Grants do not stack — the higher-privileged grant on a matching scope
     always wins (D11 clarifies this is *most-specific*, not *union*).
"""
from __future__ import annotations

from typing import Optional, Tuple

from sqlalchemy.orm import Session

from app.db.models import (
    DeviceGroupModel,
    DeviceModel,
    RoleAssignmentModel,
    SiteModel,
)


VALID_RESOURCE_TYPES = frozenset({"site", "device_group", "device"})


def effective_role(
    session: Session,
    user: dict,
    resource_type: str,
    resource_id,
) -> Optional[str]:
    """Return one of ``'super-admin' | 'admin' | 'operator' | 'observer' | None``.

    ``user`` must contain ``id`` (int) and ``is_system_admin`` (bool). The
    ``require_authenticated`` dependency in ``core/scope`` guarantees both.

    ``None`` means the caller has no matching grant *and* is not a
    system-admin. Callers translate ``None`` to 403 (or 404 when the resource
    also does not exist).
    """
    if resource_type not in VALID_RESOURCE_TYPES:
        raise ValueError(f"unknown resource_type: {resource_type!r}")

    # Fast path — system-admin bypasses all scope checks.
    if user.get("is_system_admin"):
        return "super-admin"

    user_id = user.get("id")
    if user_id is None:
        # Caller failed to enrich the user dict; treat as denied rather than
        # crashing so the 403 error surface stays clean.
        return None

    scope = _resolve_scope(session, resource_type, resource_id)
    if scope is None:
        # Resource does not exist — the caller (usually require_scope) will
        # decide whether to return 404 or 403.
        return None
    site_id, group_id = scope

    # Most-specific: exact (site, group) grant wins.
    if group_id is not None:
        row = (
            session.query(RoleAssignmentModel.role)
            .filter(
                RoleAssignmentModel.user_id == user_id,
                RoleAssignmentModel.site_id == site_id,
                RoleAssignmentModel.device_group_id == group_id,
            )
            .first()
        )
        if row is not None:
            return row[0]

    # Fall back to a site-wide grant (device_group_id IS NULL).
    row = (
        session.query(RoleAssignmentModel.role)
        .filter(
            RoleAssignmentModel.user_id == user_id,
            RoleAssignmentModel.site_id == site_id,
            RoleAssignmentModel.device_group_id.is_(None),
        )
        .first()
    )
    if row is not None:
        return row[0]

    return None


def _resolve_scope(
    session: Session,
    resource_type: str,
    resource_id,
) -> Optional[Tuple[int, Optional[int]]]:
    """Return ``(site_id, device_group_id | None)`` for the resource, or None.

    For a Device with ``device_group_id`` not yet backfilled (legacy row from
    before Phase 2), fall back to the row's ``site_id``. That fallback path
    goes away in Phase 5 with the ``site_id`` column drop.
    """
    if resource_type == "site":
        row = session.query(SiteModel.id).filter(SiteModel.id == int(resource_id)).first()
        if row is None:
            return None
        return (row[0], None)

    if resource_type == "device_group":
        row = (
            session.query(DeviceGroupModel.id, DeviceGroupModel.site_id)
            .filter(DeviceGroupModel.id == int(resource_id))
            .first()
        )
        if row is None or row[1] is None:
            # A group with site_id IS NULL cannot be scope-resolved — Phase 2
            # backfills every group; anything still null is a Phase-5 casualty.
            return None
        return (row[1], row[0])

    # device — resource_id is a device name (str)
    row = (
        session.query(
            DeviceModel.name,
            DeviceModel.device_group_id,
            DeviceModel.site_id,
            DeviceGroupModel.site_id.label("group_site_id"),
        )
        .outerjoin(
            DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id
        )
        .filter(DeviceModel.name == str(resource_id))
        .first()
    )
    if row is None:
        return None
    _, group_id, direct_site_id, group_site_id = row
    if group_id is not None and group_site_id is not None:
        return (group_site_id, group_id)
    # Legacy fallback for devices not yet placed into a group.
    if direct_site_id is not None:
        return (direct_site_id, None)
    return None
