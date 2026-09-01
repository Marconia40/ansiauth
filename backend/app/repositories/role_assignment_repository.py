"""RoleAssignmentRepository — ``scope_de()`` resolves a VisibilityScope for
a user in one query, replacing the per-check ``effective_role()`` calls the
API layer used to make. ``add()``/``get()``/``remove()`` (inherited from
Repository[T]) are the sole write door to ``role_assignments`` —
``role_assignment_service.py``'s ``grant()``/``revoke()`` route through
here now instead of their own ``session.add()``/``session.delete()``
(FINAL_ARCHITECTURE.md §6 flagged this table as having 2 competing
"sole owners"; this repository was built but never actually wired to the
live grant/revoke path until now).

``created_by_user_id``/``created_at`` are carried through the domain
object (not just the read-only fields ``scope_de()`` needs) precisely
because ``add()`` is an upsert via ``session.merge()`` — leaving them out
of ``_to_orm()`` would silently NULL them out on every role UPDATE
(``session.merge()`` copies every mapped attribute from the transient
object onto the persistent row, not just the ones the caller cared about
changing).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.core.repository import Repository
from app.db.models import RoleAssignmentModel
from app.db.session import get_session
from app.models.visibility_scope import VisibilityScope


@dataclass
class RoleAssignment:
    user_id: int
    site_id: int
    role: str
    device_group_id: "int | None" = None
    id: "int | None" = None
    created_by_user_id: "int | None" = None
    created_at: "datetime | None" = None

    def repositorio(self) -> str:
        """No es un RecursoGestionable (sin validar/reconciliar/aplicar) --
        solo para que AuditRecord.desde() etiquete el evento como
        resource="role_assignment" en vez de caer al fallback
        type(recurso).__name__.lower() ("roleassignment", sin guion bajo,
        que no matchea el string literal que ya usa
        AuditRepository._aplicar_scope() para filtrar por visibilidad)."""
        return "role_assignment"


def _to_domain(row: RoleAssignmentModel) -> RoleAssignment:
    return RoleAssignment(
        id=row.id,
        user_id=row.user_id,
        site_id=row.site_id,
        device_group_id=row.device_group_id,
        role=row.role,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
    )


def _to_orm(ra: RoleAssignment) -> RoleAssignmentModel:
    row = RoleAssignmentModel(
        id=ra.id,
        user_id=ra.user_id,
        site_id=ra.site_id,
        device_group_id=ra.device_group_id,
        role=ra.role,
        created_by_user_id=ra.created_by_user_id,
    )
    if ra.created_at is not None:
        row.created_at = ra.created_at
    return row


class RoleAssignmentRepository(Repository):
    def __init__(self):
        super().__init__(RoleAssignmentModel, _to_domain, _to_orm)

    def scope_de(self, user: dict) -> VisibilityScope:
        """One query per request produces the full grant tuple; rol_para()
        on the returned scope answers every downstream check in memory.
        """
        if user.get("is_system_admin"):
            return VisibilityScope(es_system_admin=True, grants=())
        user_id = user.get("id")
        if user_id is None:
            # Fixture/JWT-only caller without a DB row — no grants, no
            # system-admin bypass. Mirrors the "return None" behavior of
            # the old effective_role() for the same shape of caller.
            return VisibilityScope(es_system_admin=False, grants=())
        with get_session() as session:
            rows = (
                session.query(
                    RoleAssignmentModel.site_id,
                    RoleAssignmentModel.device_group_id,
                    RoleAssignmentModel.role,
                )
                .filter(RoleAssignmentModel.user_id == user_id)
                .all()
            )
        return VisibilityScope(
            es_system_admin=False,
            grants=tuple((r.site_id, r.device_group_id, r.role) for r in rows),
        )
