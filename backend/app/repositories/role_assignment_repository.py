"""RoleAssignmentRepository — sole real caller today is ``scope_de()``,
which resolves a VisibilityScope for a user in one query and replaces the
per-check ``effective_role()`` calls the API layer used to make.

The generic get/add/list/remove inherited from Repository[T] rely on the
trivial mappers below. They exist only so the class is a valid
Repository[T] subclass — no code path uses them yet. When a future phase
migrates ``role_assignment_service.py`` off its own SQL, it will be the
first real caller of add()/list()/remove() and the mappers can be
promoted to a full domain model then.
"""
from __future__ import annotations

from dataclasses import dataclass

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


def _to_domain(row: RoleAssignmentModel) -> RoleAssignment:
    return RoleAssignment(
        id=row.id,
        user_id=row.user_id,
        site_id=row.site_id,
        device_group_id=row.device_group_id,
        role=row.role,
    )


def _to_orm(ra: RoleAssignment) -> RoleAssignmentModel:
    return RoleAssignmentModel(
        id=ra.id,
        user_id=ra.user_id,
        site_id=ra.site_id,
        device_group_id=ra.device_group_id,
        role=ra.role,
    )


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
