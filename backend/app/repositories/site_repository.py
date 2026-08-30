"""SiteRepository — 7ma subclase de Repository[T], Fase 6 A3.

Justificación distinta a las otras 6 (que existen por un JOIN/consulta que
``filter_by()`` no puede expresar): ``crear_con_grupo_default()`` no es un
alta de una sola entidad — crea el ``Site`` **y** su ``DeviceGroup``
Default **y** actualiza ``Site.default_group_id``, las 3 cosas atómicas en
una sola transacción. ``Repository[Site].add()`` genérico (upsert de una
fila) no alcanza — mismo criterio de fondo (Repository[T] genérico no
cubre esto), aplicado a una operación transaccional en vez de a una
consulta.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from app.core.exceptions import SiteHasDevicesError
from app.core.repository import Repository
from app.db.models import DeviceGroupModel, DeviceModel, SiteModel
from app.db.session import get_session
from app.models.site import Site

if TYPE_CHECKING:
    from app.models.device_group import DeviceGroup
    from app.models.visibility_scope import VisibilityScope

DEFAULT_GROUP_NAME = "Default"
BASE_INFRA_SITE_KIND = "BASE_INFRASTRUCTURE"


def _to_domain(row: SiteModel) -> Site:
    return Site(
        id=row.id,
        name=row.name,
        description=row.description,
        kind=row.kind,
        default_group_id=row.default_group_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_orm(s: Site) -> SiteModel:
    return SiteModel(
        id=s.id, name=s.name, description=s.description, kind=s.kind,
        default_group_id=s.default_group_id,
    )


def _default_group_name_for_site(session, site_id: int) -> str:
    """Copia site_service.py: _default_group_name_for_site() tal cual --
    caso raro: un grupo ya llamado "Default" preexistente en este site
    (UNIQUE(site_id, name) colisionaría con el auto-provisioning)."""
    existing = (
        session.query(DeviceGroupModel)
        .filter_by(name=DEFAULT_GROUP_NAME, site_id=site_id)
        .first()
    )
    if existing is None:
        return DEFAULT_GROUP_NAME
    return f"{DEFAULT_GROUP_NAME} (site {site_id})"


class SiteRepository(Repository):
    def __init__(self):
        super().__init__(SiteModel, _to_domain, _to_orm)

    def crear_con_grupo_default(
        self, name: str, description: Optional[str] = None, *, kind: str = "REGULAR",
    ) -> Site:
        """Reemplaza site_service.py: create_site() (Site+DeviceGroup Default
        atómico) Y ensure_base_infrastructure() (idempotente -- si ya existe
        un Site kind='BASE_INFRASTRUCTURE', no crea uno nuevo, solo asegura
        que tenga su Default group)."""
        with get_session() as session:
            if kind == BASE_INFRA_SITE_KIND:
                existente = session.query(SiteModel).filter_by(kind=kind).first()
                if existente is not None:
                    return self._asegurar_grupo_default(session, existente)
            elif session.query(SiteModel).filter_by(name=name).first():
                raise ValueError(f"Site '{name}' already exists")
            row = SiteModel(name=name, description=description, kind=kind)
            session.add(row)
            session.flush()  # popula row.id antes de referenciarlo abajo
            grupo = DeviceGroupModel(
                name=_default_group_name_for_site(session, row.id),
                site_id=row.id,
                is_default=True,
                description=f"Default group for site '{name}'.",
            )
            session.add(grupo)
            session.flush()
            row.default_group_id = grupo.id
            session.flush()
            return _to_domain(row)

    def _asegurar_grupo_default(self, session, row: SiteModel) -> Site:
        """Rama idempotente de crear_con_grupo_default() para
        kind='BASE_INFRASTRUCTURE': si el Site ya existe pero le falta
        default_group_id (crash a mitad de un boot anterior), lo completa
        en vez de duplicar el Site."""
        if row.default_group_id is None:
            existing_default = (
                session.query(DeviceGroupModel)
                .filter_by(site_id=row.id, is_default=True)
                .first()
            )
            if existing_default is None:
                group = DeviceGroupModel(
                    name=DEFAULT_GROUP_NAME, site_id=row.id, is_default=True,
                    description=f"Default group for {row.name}.",
                )
                session.add(group)
                session.flush()
                row.default_group_id = group.id
            else:
                row.default_group_id = existing_default.id
            session.flush()
        return _to_domain(row)

    def grupo_default(self, site: Site) -> "DeviceGroup":
        """Resuelve el DeviceGroup Default de *site* -- necesario en
        Inventory.register()/move() (A5) para resolver `device_group_id=None`
        (D8). SiteRepository ya toca DeviceGroupModel directo en
        crear_con_grupo_default()/contar_devices() -- mismo criterio acá, no
        hace falta inyectar DeviceGroupRepository para esto."""
        from app.models.device_group import DeviceGroup

        if site.default_group_id is None:
            raise ValueError(f"Site {site.id} has no Default group")
        with get_session() as session:
            row = (
                session.query(DeviceGroupModel)
                .filter_by(id=site.default_group_id)
                .first()
            )
            if row is None:
                raise ValueError(
                    f"Site {site.id}'s Default group {site.default_group_id} not found"
                )
            return DeviceGroup(
                id=row.id, name=row.name, description=row.description,
                site_id=row.site_id, es_default=row.is_default, created_at=row.created_at,
            )

    def visibles(self, scope: "VisibilityScope") -> list[Site]:
        """Reemplaza site_service.py: list_sites_for_user() -- system-admin ve
        todos; el resto ve todo site con AL MENOS un grant (site-wide o de
        grupo específico -- por eso usa scope.grants directo, no
        scope.site_ids, que el propio docstring de VisibilityScope avisa que
        excluye grants solo-de-grupo). Base-Infrastructure se oculta a
        no-system-admins (D14)."""
        with get_session() as session:
            q = session.query(SiteModel)
            if not scope.es_system_admin:
                granted_site_ids = {sid for sid, _gid, _role in scope.grants}
                if not granted_site_ids:
                    return []
                q = q.filter(SiteModel.id.in_(granted_site_ids))
                q = q.filter(SiteModel.kind != BASE_INFRA_SITE_KIND)
            rows = q.order_by(SiteModel.name).all()
            return [_to_domain(r) for r in rows]

    def contar_devices(self, site_id: int) -> int:
        with get_session() as session:
            return (
                session.query(DeviceModel)
                .join(DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id)
                .filter(DeviceGroupModel.site_id == site_id)
                .count()
            )

    def tiene_devices(self, site_id: int) -> bool:
        return self.contar_devices(site_id) > 0

    def eliminar(self, site_id: int) -> bool:
        """Copia site_service.py: delete_site() tal cual -- el orden de
        operaciones (null default_group_id -> borrar grupos -> borrar site)
        importa por el RESTRICT FK real, no reordenar."""
        with get_session() as session:
            row = session.query(SiteModel).filter_by(id=site_id).first()
            if not row:
                return False
            if row.kind == BASE_INFRA_SITE_KIND:
                raise ValueError(
                    "The Base-Infrastructure site is system-managed and cannot be deleted"
                )
            device_count = (
                session.query(DeviceModel)
                .join(DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id)
                .filter(DeviceGroupModel.site_id == site_id)
                .count()
            )
            if device_count > 0:
                raise SiteHasDevicesError(site_id, device_count)
            row.default_group_id = None
            session.flush()
            session.query(DeviceGroupModel).filter_by(site_id=site_id).delete(
                synchronize_session=False
            )
            session.flush()
            session.delete(row)
        return True
