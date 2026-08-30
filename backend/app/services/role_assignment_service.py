"""MSP: Phase 3 — RoleAssignmentService.

Sole owner of the ``role_assignments`` table and the ``users.is_system_admin``
column. Endpoints delegate to the four methods below; no other module writes
to either surface.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException

from app.db.models import (
    DeviceGroupModel,
    RoleAssignmentModel,
    SiteModel,
    UserModel,
)
from app.db.session import get_session
from app.models.audit import AuditRecord
from app.schemas.role_assignment import RoleAssignmentRead

logger = logging.getLogger(__name__)

_VALID_ASSIGNMENT_ROLES = frozenset({"observer", "operator", "admin"})


def _to_read(row: RoleAssignmentModel) -> RoleAssignmentRead:
    return RoleAssignmentRead(
        id=row.id,
        user_id=row.user_id,
        site_id=row.site_id,
        site_name=row.site.name if row.site is not None else None,
        device_group_id=row.device_group_id,
        device_group_name=(
            row.device_group.name if row.device_group is not None else None
        ),
        role=row.role,
        created_at=row.created_at,
        created_by_user_id=row.created_by_user_id,
        created_by_username=(
            row.created_by.username if row.created_by is not None else None
        ),
    )


class RoleAssignmentService:
    """Sole owner of role_assignments rows and users.is_system_admin."""

    def __init__(self, db=None):
        self._db = db  # session-per-call; kept for plan compatibility

    # ─── Grant / revoke ──────────────────────────────────────────────────

    def grant(
        self,
        *,
        target_user_id: int,
        site_id: int,
        device_group_id: Optional[int],
        role: str,
        actor: dict,
    ) -> RoleAssignmentRead:
        """Create (or return) a role grant at the given scope.

        Authz (D25):
          * system-admin actor → allowed to grant any role anywhere.
          * site-admin actor (an admin grant on ``site_id``) → may grant
            observer/operator/admin on *that* site.
          * group-admin actor (an admin grant on a specific group of
            ``site_id``) → may only grant observer/operator within their
            group; **may not grant admin** and may not create site-wide
            grants (device_group_id must equal their scope group).
        """
        if role not in _VALID_ASSIGNMENT_ROLES:
            raise ValueError(
                f"role must be one of {sorted(_VALID_ASSIGNMENT_ROLES)}"
            )
        with get_session() as session:
            target_user = session.query(UserModel).filter_by(id=target_user_id).first()
            if target_user is None:
                raise HTTPException(status_code=404, detail=f"User {target_user_id} not found")
            site = session.query(SiteModel).filter_by(id=site_id).first()
            if site is None:
                raise HTTPException(status_code=404, detail=f"Site {site_id} not found")
            if device_group_id is not None:
                group = (
                    session.query(DeviceGroupModel)
                    .filter_by(id=device_group_id)
                    .first()
                )
                if group is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Device group {device_group_id} not found",
                    )
                if group.site_id != site_id:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Device group {device_group_id} belongs to site "
                            f"{group.site_id}, not site {site_id}"
                        ),
                    )
            self._authorize_grant_or_revoke(
                session, actor=actor, site_id=site_id,
                device_group_id=device_group_id, role_being_granted=role,
            )
            existing = (
                session.query(RoleAssignmentModel)
                .filter(
                    RoleAssignmentModel.user_id == target_user_id,
                    RoleAssignmentModel.site_id == site_id,
                    RoleAssignmentModel.device_group_id == device_group_id,
                )
                .first()
            )
            if existing is not None:
                # Audita siempre, incluso cuando el rol no cambia -- corrección
                # real aplicada acá (Fase 7, §1.5): FINAL_ARCHITECTURE.md §6
                # ("Mismo patrón, 3ra vez") ya había marcado esto como
                # pendiente desde antes de que existiera AuditRepository --
                # el código real solo auditaba si `existing.role != role`, un
                # re-grant del mismo rol quedaba sin ningún registro. Mismo
                # criterio ya aplicado a Inventory.move() (Fase 6): noop
                # idempotente, pero SÍ auditado, con "noop": True en el payload.
                noop = existing.role == role
                if not noop:
                    existing.role = role
                    session.flush()
                record = _to_read(existing)
                from app.composition import audit_repository
                audit_repository.append(AuditRecord(
                    user=(actor.get("username") if actor else None) or "system",
                    action="update_role_assignment",
                    resource="role_assignment",
                    resource_id=str(existing.id),
                    details={
                        "user_id": target_user_id, "site_id": site_id,
                        "device_group_id": device_group_id, "role": role,
                        "noop": noop,
                    },
                ))
                return record
            row = RoleAssignmentModel(
                user_id=target_user_id,
                site_id=site_id,
                device_group_id=device_group_id,
                role=role,
                created_by_user_id=actor.get("id") if actor else None,
            )
            session.add(row)
            session.flush()
            record = _to_read(row)
        from app.composition import audit_repository
        audit_repository.append(AuditRecord(
            user=(actor.get("username") if actor else None) or "system",
            action="grant_role_assignment",
            resource="role_assignment",
            resource_id=str(record.id),
            details={
                "target_user_id": target_user_id,
                "site_id": site_id,
                "device_group_id": device_group_id,
                "role": role,
            },
        ))
        return record

    def revoke(self, grant_id: int, actor: dict) -> None:
        """Delete a grant. Same authz as :py:meth:`grant`."""
        with get_session() as session:
            row = (
                session.query(RoleAssignmentModel)
                .filter_by(id=grant_id)
                .first()
            )
            if row is None:
                raise HTTPException(status_code=404, detail=f"Grant {grant_id} not found")
            self._authorize_grant_or_revoke(
                session, actor=actor, site_id=row.site_id,
                device_group_id=row.device_group_id,
                role_being_granted=row.role,
            )
            details = {
                "grant_id": grant_id,
                "user_id": row.user_id,
                "site_id": row.site_id,
                "device_group_id": row.device_group_id,
                "role": row.role,
            }
            session.delete(row)
        from app.composition import audit_repository
        audit_repository.append(AuditRecord(
            user=(actor.get("username") if actor else None) or "system",
            action="revoke_role_assignment",
            resource="role_assignment",
            resource_id=str(grant_id),
            details=details,
        ))

    def list_for_user(
        self,
        user_id: int,
        viewer: dict,
    ) -> list[RoleAssignmentRead]:
        """Return grants held by ``user_id`` filtered by what ``viewer`` may see.

        Viewer visibility:
          * system-admin: every grant.
          * viewer.id == user_id: every own grant.
          * otherwise: only grants at sites where the viewer holds an admin
            grant (site-wide or group-specific).
        """
        with get_session() as session:
            if not session.query(UserModel.id).filter_by(id=user_id).first():
                raise HTTPException(status_code=404, detail=f"User {user_id} not found")
            q = session.query(RoleAssignmentModel).filter_by(user_id=user_id)
            rows = q.all()
            if viewer.get("is_system_admin") or viewer.get("id") == user_id:
                return [_to_read(r) for r in rows]
            viewer_id = viewer.get("id")
            if viewer_id is None:
                return []
            # Sites where viewer is admin — union of site-wide admin and
            # group-scoped admin (group-admins can see their own scope's
            # peer grants inside the same group).
            admin_grants = (
                session.query(
                    RoleAssignmentModel.site_id,
                    RoleAssignmentModel.device_group_id,
                )
                .filter(
                    RoleAssignmentModel.user_id == viewer_id,
                    RoleAssignmentModel.role == "admin",
                )
                .all()
            )
            visible_sites = {sid for (sid, gid) in admin_grants if gid is None}
            visible_groups = {gid for (_sid, gid) in admin_grants if gid is not None}
            filtered = [
                r for r in rows
                if r.site_id in visible_sites
                or (r.device_group_id is not None and r.device_group_id in visible_groups)
            ]
            return [_to_read(r) for r in filtered]

    def set_system_admin(
        self,
        target_user_id: int,
        is_system_admin: bool,
        actor: dict,
    ) -> None:
        """Toggle ``users.is_system_admin``. Guards the last-system-admin exit."""
        if not actor.get("is_system_admin"):
            raise HTTPException(
                status_code=403,
                detail="Only system-admins may modify the system-admin flag",
            )
        with get_session() as session:
            row = session.query(UserModel).filter_by(id=target_user_id).first()
            if row is None:
                raise HTTPException(status_code=404, detail=f"User {target_user_id} not found")
            if not is_system_admin and row.is_system_admin:
                # Do not allow demoting the last active system-admin.
                remaining = (
                    session.query(UserModel)
                    .filter(
                        UserModel.is_system_admin.is_(True),
                        UserModel.is_active.is_(True),
                        UserModel.id != target_user_id,
                    )
                    .count()
                )
                if remaining == 0:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            "Cannot demote the last active system-admin — "
                            "promote another user first"
                        ),
                    )
            row.is_system_admin = bool(is_system_admin)
        from app.composition import audit_repository
        audit_repository.append(AuditRecord(
            user=(actor.get("username") if actor else None) or "system",
            action="set_system_admin",
            resource="user",
            resource_id=str(target_user_id),
            details={"is_system_admin": is_system_admin},
        ))

    # ─── Authorization helper ────────────────────────────────────────────

    def _authorize_grant_or_revoke(
        self,
        session,
        *,
        actor: dict,
        site_id: int,
        device_group_id: Optional[int],
        role_being_granted: str,
    ) -> None:
        """Enforce D25: only system-admins and site-admins may issue grants.

        Group-admins may not delegate (they cannot grant *any* role — the
        privilege to grant lives at the site level and above).
        """
        if actor.get("is_system_admin"):
            return
        actor_id = actor.get("id")
        if actor_id is None:
            raise HTTPException(
                status_code=403,
                detail="Grant operations require a fully-authenticated actor",
            )
        # Does the actor hold a site-wide admin grant on this site?
        site_admin = (
            session.query(RoleAssignmentModel.id)
            .filter(
                RoleAssignmentModel.user_id == actor_id,
                RoleAssignmentModel.site_id == site_id,
                RoleAssignmentModel.device_group_id.is_(None),
                RoleAssignmentModel.role == "admin",
            )
            .first()
        )
        if site_admin is not None:
            return
        raise HTTPException(
            status_code=403,
            detail=(
                "Grant operations require system-admin or a site-wide admin "
                f"grant on site {site_id} (group-admins may not delegate — D25)"
            ),
        )
