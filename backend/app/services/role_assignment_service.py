"""MSP: Phase 3 — RoleAssignmentService.

Owns the authorization/audit rules around granting and revoking roles, and
the ``users.is_system_admin`` column — but ``role_assignments`` itself is
written exclusively through ``RoleAssignmentRepository`` (``add()``/
``remove()``), not through a session opened here. This service used to
write ``RoleAssignmentModel`` directly (``session.add()``/``session.delete()``),
which meant 2 competing "sole owners" of the same table existed at once —
the repository sat unused (its own docstring admitted it) while this
service did the real writes. Fixed by routing through the repository, per
FINAL_ARCHITECTURE.md §6.
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
from app.models.domain_event import DomainEvent
from app.repositories.role_assignment_repository import RoleAssignment
from app.schemas.role_assignment import _VALID_ASSIGNMENT_ROLES, RoleAssignmentRead

logger = logging.getLogger(__name__)


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


def _leer(grant_id: int) -> RoleAssignmentRead:
    """Lee el grant recién escrito con sus 3 joins de display (site_name/
    device_group_name/created_by_username) -- concern de lectura separado
    de la escritura (que sí pasa por RoleAssignmentRepository); el dominio
    de RoleAssignmentRepository no carga esos nombres, solo los ids."""
    with get_session() as session:
        row = session.query(RoleAssignmentModel).filter_by(id=grant_id).first()
        return _to_read(row)


class RoleAssignmentService:
    """Reglas de autorización/auditoría para grant/revoke -- la escritura
    real de role_assignments vive en RoleAssignmentRepository."""

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

        Authz (D25), enforced by ``_authorize_grant_or_revoke()``:
          * system-admin actor → allowed to grant any role anywhere.
          * site-admin actor (an admin grant on ``site_id``, site-wide —
            ``device_group_id=None``) → may grant any role at any scope
            within that site.
          * any other actor, including a group-admin (an admin grant
            scoped to one ``device_group_id`` rather than the whole site)
            → rejected with 403. Group-admins cannot delegate: the
            privilege to grant lives at the site level and above, per D25.

        Correction: this docstring previously described a 3rd tier
        ("group-admin actor... may grant observer/operator within their
        group") that was never implemented — found reviewing this file
        against its own enforcement method, whose docstring states the
        opposite. The actual behavior (the more restrictive one) was
        always correct; only this docstring was wrong.
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
            actor=actor, site_id=site_id,
            device_group_id=device_group_id, role_being_granted=role,
        )
        from app.composition import event_dispatcher, role_assignment_repository

        actor_username = (actor.get("username") if actor else None) or "system"
        existentes = role_assignment_repository.list(
            user_id=target_user_id, site_id=site_id, device_group_id=device_group_id,
        )
        existing = existentes[0] if existentes else None
        if existing is not None:
            # Audita siempre, incluso cuando el rol no cambia -- corrección
            # real aplicada acá (Fase 7, §1.5): FINAL_ARCHITECTURE.md §6
            # ("Mismo patrón, 3ra vez") ya había marcado esto como
            # pendiente desde antes de que existiera AuditRepository -- el
            # código real solo auditaba si `existing.role != role`, un
            # re-grant del mismo rol quedaba sin ningún registro. Mismo
            # criterio ya aplicado a Inventory.move() (Fase 6): noop
            # idempotente, pero SÍ auditado, con "noop": True en el payload.
            noop = existing.role == role
            if not noop:
                existing.role = role
                role_assignment_repository.add(existing)
            record = _leer(existing.id)
            # Antes: audit_repository.append(AuditRecord(...)) directo --
            # único par de writes de esta clase que se saltaba
            # EventDispatcher (Inventory ya despacha DomainEvent para su
            # ciclo de vida). resource_id va en el payload -- ver
            # AuditRecord.desde() (models/audit.py).
            event_dispatcher.despachar([DomainEvent(
                "update_role_assignment", existing, None, actor_username,
                {
                    "resource_id": existing.id,
                    "user_id": target_user_id, "site_id": site_id,
                    "device_group_id": device_group_id, "role": role,
                    "noop": noop,
                },
            )])
            return record
        nuevo = RoleAssignment(
            user_id=target_user_id, site_id=site_id, device_group_id=device_group_id,
            role=role, created_by_user_id=actor.get("id") if actor else None,
        )
        creado = role_assignment_repository.add(nuevo)
        record = _leer(creado.id)
        event_dispatcher.despachar([DomainEvent(
            "grant_role_assignment", creado, None, actor_username,
            {
                "resource_id": creado.id,
                "target_user_id": target_user_id,
                "site_id": site_id,
                "device_group_id": device_group_id,
                "role": role,
            },
        )])
        return record

    def revoke(self, grant_id: int, actor: dict) -> None:
        """Delete a grant. Same authz as :py:meth:`grant`."""
        from app.composition import event_dispatcher, role_assignment_repository

        existing = role_assignment_repository.get(grant_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"Grant {grant_id} not found")
        self._authorize_grant_or_revoke(
            actor=actor, site_id=existing.site_id,
            device_group_id=existing.device_group_id,
            role_being_granted=existing.role,
        )
        details = {
            "resource_id": grant_id,
            "grant_id": grant_id,
            "user_id": existing.user_id,
            "site_id": existing.site_id,
            "device_group_id": existing.device_group_id,
            "role": existing.role,
        }
        role_assignment_repository.remove(grant_id)
        actor_username = (actor.get("username") if actor else None) or "system"
        event_dispatcher.despachar([DomainEvent(
            "revoke_role_assignment", existing, None, actor_username, details,
        )])

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
        # Mismo criterio que grant()/revoke() -- despacha vía EventDispatcher
        # en vez de audit_repository.append() directo. `recurso` es el User
        # de dominio (no la fila ORM) -- su nombre de clase ya lowercasea a
        # "user", el resource string que esto necesita, sin agregar un
        # repositorio() artificial.
        from app.composition import event_dispatcher, user_repository

        target_user = user_repository.get(target_user_id)
        event_dispatcher.despachar([DomainEvent(
            "set_system_admin", target_user, None,
            (actor.get("username") if actor else None) or "system",
            {"resource_id": target_user_id, "is_system_admin": is_system_admin},
        )])

    # ─── Authorization helper ────────────────────────────────────────────

    def _authorize_grant_or_revoke(
        self,
        *,
        actor: dict,
        site_id: int,
        device_group_id: Optional[int],
        role_being_granted: str,
    ) -> None:
        """Enforce D25: only system-admins and site-admins may issue grants.

        Group-admins may not delegate (they cannot grant *any* role — the
        privilege to grant lives at the site level and above).

        Antes reimplementaba acá su propia query "¿tiene el actor un grant
        admin site-wide?" en vez de reusar RoleAssignmentRepository.scope_de()
        -- 2da forma de responder la misma pregunta que ya contesta
        VisibilityScope.rol_para() en el resto de la API (FINAL_ARCHITECTURE.md
        §6). ``rol_para(site_id, None)`` -- con ``device_group_id=None`` a
        propósito, no el ``device_group_id`` del grant que se está creando --
        replica exactamente el filtro ``device_group_id IS NULL`` de la query
        vieja: un admin de GRUPO nunca cuenta acá, solo site-wide o
        system-admin (ya cortado arriba)."""
        if actor.get("is_system_admin"):
            return
        actor_id = actor.get("id")
        if actor_id is None:
            raise HTTPException(
                status_code=403,
                detail="Grant operations require a fully-authenticated actor",
            )
        from app.composition import role_assignment_repository

        scope = role_assignment_repository.scope_de(actor)
        if scope.rol_para(site_id, None) == "admin":
            return
        raise HTTPException(
            status_code=403,
            detail=(
                "Grant operations require system-admin or a site-wide admin "
                f"grant on site {site_id} (group-admins may not delegate — D25)"
            ),
        )
