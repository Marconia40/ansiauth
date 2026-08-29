"""DeviceGroupRepository — sixth Repository[T] subclass, added for the
one scoped read AuditRepository.query() cannot answer from VisibilityScope
alone.

grupos_visibles(scope) unions the caller's direct group grants with the
groups whose site has a site-wide grant. This mirrors the
``visible_group_ids`` computation inside _apply_msp_audit_scoping and
lets an audit filter on ``resource='device_group'`` list every group
the caller can see — not only the ones granted explicitly.

to_domain / to_orm stay None on purpose: the DeviceGroup domain
dataclass does not exist yet in this plan (device_group_service.py works
with DeviceGroupRead + ORM rows directly). Fase 6 constructs the
entity and can wire the mappers then. The class still inherits from
Repository[T] because the concept of a device group is a real entity
with individual identity — the criterion from FASE_3.md §B2.
"""
from __future__ import annotations

from app.core.repository import Repository
from app.db.models import DeviceGroupModel
from app.db.session import get_session
from app.models.visibility_scope import VisibilityScope


class DeviceGroupRepository(Repository):
    def __init__(self):
        super().__init__(DeviceGroupModel, to_domain=None, to_orm=None)

    def grupos_visibles(self, scope: VisibilityScope) -> set[int]:
        """IDs of DeviceGroup visible to *scope*: direct group grants
        UNION groups whose site has a site-wide grant.

        Returns an empty set for system-admin — the sole caller today,
        AuditRepository.query(), short-circuits on system-admin before
        reaching here.
        """
        if scope.es_system_admin:
            return set()
        directos = set(scope.device_group_ids)
        if not scope.site_ids:
            return directos
        with get_session() as session:
            via_site = {
                r[0]
                for r in session.query(DeviceGroupModel.id)
                .filter(DeviceGroupModel.site_id.in_(scope.site_ids))
                .all()
            }
        return directos | via_site
