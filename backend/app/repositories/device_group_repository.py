"""DeviceGroupRepository — sixth Repository[T] subclass, added for the
one scoped read AuditRepository.query() cannot answer from VisibilityScope
alone.

grupos_visibles(scope) unions the caller's direct group grants with the
groups whose site has a site-wide grant. This mirrors the
``visible_group_ids`` computation inside _apply_msp_audit_scoping and
lets an audit filter on ``resource='device_group'`` list every group
the caller can see — not only the ones granted explicitly.

``to_domain``/``to_orm`` wired in Fase 6 (A2/A4) against ``app.models.
device_group.DeviceGroup``, now that the entity exists. The class still
inherits from Repository[T] because the concept of a device group is a
real entity with individual identity — the criterion from FASE_3.md §B2.
"""
from __future__ import annotations

from app.core.repository import Repository
from app.db.models import DeviceGroupModel
from app.db.session import get_session
from app.models.device_group import DeviceGroup
from app.models.visibility_scope import VisibilityScope


def _to_domain(row: DeviceGroupModel) -> DeviceGroup:
    return DeviceGroup(
        id=row.id, name=row.name, description=row.description,
        site_id=row.site_id, es_default=row.is_default, created_at=row.created_at,
    )


def _to_orm(g: DeviceGroup) -> DeviceGroupModel:
    return DeviceGroupModel(
        id=g.id, name=g.name, description=g.description,
        site_id=g.site_id, is_default=g.es_default,
    )


class DeviceGroupRepository(Repository):
    def __init__(self):
        super().__init__(DeviceGroupModel, _to_domain, _to_orm)

    def eliminar_con_auto_move(self, group_id: int, actor: dict, inventory: "Inventory") -> "dict | None":
        """Copia device_group_service.py: delete_group() tal cual -- bloquea
        si es_default (D7), si no, mueve cada device miembro al Default del
        site vía inventory.move(enforce_authz=False) (D19), después borra el
        grupo. Recibe Inventory inyectado -- no lo importa directo, mismo
        criterio de inyección por constructor que el resto del catálogo.

        Devuelve None si el grupo no existe. ``{"deleted_group_id": ...,
        "moved_devices": [...]}`` en éxito -- mismo shape que el real."""
        from app.core.exceptions import DefaultGroupImmutableError
        from app.db.models import DeviceModel, SiteModel

        with get_session() as session:
            row = session.query(DeviceGroupModel).filter_by(id=group_id).first()
            if row is None:
                return None
            if row.is_default:
                raise DefaultGroupImmutableError(
                    f"Group {group_id} is a Site's Default group and cannot be deleted (D7)"
                )
            site_id = row.site_id
            default_group_id = (
                session.query(SiteModel.default_group_id)
                .filter(SiteModel.id == site_id)
                .scalar()
            )
            if default_group_id is None:
                raise ValueError(
                    f"Site {site_id} has no Default group; cannot auto-move members"
                )
            members: set[str] = {
                r[0]
                for r in session.query(DeviceModel.name)
                .filter_by(device_group_id=group_id)
                .all()
            }
        # Cada move corre su propia transacción (vía inventory.move()) para
        # que las filas de audit queden independientes -- el borrado del
        # grupo va al final, mismo orden que el real.
        moved: list[str] = []
        for name in sorted(members):
            inventory.move(
                name,
                target_group_id=default_group_id,
                actor=actor or {"username": "system", "id": None, "is_system_admin": True},
                reason="auto_move_on_group_delete",
                enforce_authz=False,
            )
            moved.append(name)
        with get_session() as session:
            row = session.query(DeviceGroupModel).filter_by(id=group_id).first()
            if row is None:
                # Alguien más ya lo borró -- está bien.
                return {"deleted_group_id": group_id, "moved_devices": moved}
            session.delete(row)
        return {"deleted_group_id": group_id, "moved_devices": moved}

    def contar_miembros(self, group_id: int) -> int:
        from app.db.models import DeviceModel

        with get_session() as session:
            return session.query(DeviceModel).filter_by(device_group_id=group_id).count()

    def contar_miembros_batch(self, group_ids: list[int]) -> dict[int, int]:
        """Mismo conteo que ``contar_miembros()``, para varios grupos en 1
        sola query (GROUP BY) en vez de N -- ``list_groups()``/
        ``list_groups_for_site()`` hacían 1 query de conteo por grupo
        devuelto (N+1 real, encontrado en una revisión de código). Grupos
        sin ningún device no aparecen en el resultado -- el caller debe
        usar ``.get(group_id, 0)``."""
        from sqlalchemy import func

        from app.db.models import DeviceModel

        if not group_ids:
            return {}
        with get_session() as session:
            rows = (
                session.query(DeviceModel.device_group_id, func.count(DeviceModel.id))
                .filter(DeviceModel.device_group_id.in_(group_ids))
                .group_by(DeviceModel.device_group_id)
                .all()
            )
            return {group_id: count for group_id, count in rows}

    def en_site(self, site_id: int) -> list[DeviceGroup]:
        with get_session() as session:
            rows = (
                session.query(DeviceGroupModel)
                .filter_by(site_id=site_id)
                .order_by(DeviceGroupModel.name)
                .all()
            )
            return [_to_domain(r) for r in rows]

    def visibles_para_usuario(self, scope: VisibilityScope) -> list[DeviceGroup]:
        """Reemplaza device_group_service.py: list_groups_for_user() -- un
        caller ve un grupo cuando tiene CUALQUIER grant sobre su site
        (site-wide, scope.site_ids) o un grant específico sobre el grupo
        (scope.device_group_ids). Base-Infrastructure oculto a
        no-system-admins (D14)."""
        with get_session() as session:
            q = session.query(DeviceGroupModel)
            if not scope.es_system_admin:
                site_ids = scope.site_ids or set()
                group_ids = scope.device_group_ids
                if not site_ids and not group_ids:
                    return []
                from sqlalchemy import or_

                conds = []
                if site_ids:
                    conds.append(DeviceGroupModel.site_id.in_(site_ids))
                if group_ids:
                    conds.append(DeviceGroupModel.id.in_(group_ids))
                q = q.filter(or_(*conds))
                from app.db.models import SiteModel

                hidden_site_ids = {
                    r[0]
                    for r in session.query(SiteModel.id)
                    .filter(SiteModel.kind == "BASE_INFRASTRUCTURE")
                    .all()
                }
                if hidden_site_ids:
                    q = q.filter(~DeviceGroupModel.site_id.in_(hidden_site_ids))
            rows = q.order_by(DeviceGroupModel.name).all()
            return [_to_domain(r) for r in rows]

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
