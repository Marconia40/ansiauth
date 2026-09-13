import logging

from fastapi import APIRouter, Depends

from app.core.exceptions import ConflictError, NotFoundError, SiteHasDevicesError, ValidationError
from app.core.response import ok
from app.core.scope import (
    obtener_scope,
    require_authenticated,
    require_elevated,
    require_scope,
    require_system_admin,
    visible_or_404,
)
from app.models.audit import AuditRecord
from app.models.domain_event import DomainEvent
from app.models.visibility_scope import VisibilityScope
from app.repositories.site_repository import BASE_INFRA_SITE_KIND, DEFAULT_GROUP_NAME
from app.schemas.site import SiteCreate, SiteRead, SiteUpdate

logger = logging.getLogger(__name__)
router = APIRouter()


_UNSET = object()


def _to_read(site, *, device_count=_UNSET) -> dict:
    """*device_count*, when given, skips this function's own per-item
    count query -- list_sites() batches it across every site in one pass
    instead of paying for it once per row (N+1 real, found in a code
    review). Single-item callers (get_site(), create_site(), etc.) don't
    pass it, so they still get the same 1 query as before."""
    from app.composition import site_repository

    if device_count is _UNSET:
        device_count = site_repository.contar_devices(site.id)
    return SiteRead(
        id=site.id, name=site.name, description=site.description,
        created_at=site.created_at, updated_at=site.updated_at,
        device_count=device_count,
    ).model_dump()


@router.post(
    "/",
    summary="Create site",
    description=(
        "Create a named site with its Default group. Names are trimmed and must "
        "be unique. Requires system-admin."
    ),
)
def create_site(
    data: SiteCreate,
    current_user: dict = Depends(require_system_admin),
):
    from app.composition import audit_repository, event_dispatcher, site_repository

    try:
        site = site_repository.crear_con_grupo_default(name=data.name, description=data.description)
    except ValueError as e:
        raise ValidationError(str(e))
    event_dispatcher.despachar([DomainEvent(
        "create_site", site, None, current_user["username"],
        {"resource_id": site.id, "name": site.name},
    )])
    # Segunda fila de auditoría (bootstrap del grupo Default) se queda en el
    # camino directo -- crear_con_grupo_default() no devuelve el DeviceGroup
    # creado, no hay `recurso` real para pasarle a DomainEvent sin ampliar
    # esa firma solo para esto (mismo criterio que jobs.py:cancel_job).
    audit_repository.append(AuditRecord(
        user=current_user["username"],
        action="create_device_group",
        resource="device_group",
        # resource_id faltaba -- bug real encontrado en una revisión de
        # código: _aplicar_scope() filtra filas resource="device_group" por
        # resource_id IN (grupos visibles), y None nunca matchea un IN --
        # esta fila quedaba invisible para cualquier admin de ese grupo,
        # a diferencia de cualquier otra fila device_group (creada vía
        # DomainEvent, que sí setea resource_id=group.id). site.default_
        # group_id ya está poblado acá (crear_con_grupo_default() lo setea),
        # no hace falta el objeto DeviceGroup completo para esto.
        resource_id=str(site.default_group_id),
        details={
            "name": DEFAULT_GROUP_NAME,
            "site_id": site.id,
            "is_default": True,
            "reason": "site_bootstrap",
        },
    ))
    return ok(_to_read(site))


@router.get(
    "/",
    summary="List sites",
    description=(
        "Return sites visible to the caller. Reads from role_assignments and "
        "hides Base-Infrastructure from non system-admins (D14)."
    ),
)
def list_sites(
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import site_repository

    sites = site_repository.visibles(scope)
    device_counts = site_repository.contar_devices_batch([s.id for s in sites])
    return ok([_to_read(s, device_count=device_counts.get(s.id, 0)) for s in sites])


@router.get(
    "/{site_id}",
    summary="Get site",
    description="Return a single site by ID.",
)
def get_site(
    site_id: int,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import site_repository

    site = site_repository.get(site_id)
    if not site:
        raise NotFoundError(f"Site {site_id} not found")
    # D14: Base-Infrastructure is invisible to non system-admins even if a
    # stray grant somehow lands on it.
    if site.kind == BASE_INFRA_SITE_KIND and not current_user.get("is_system_admin"):
        raise NotFoundError(f"Site {site_id} not found")
    visible_or_404(scope, site_id, "site", "observer", f"Site {site_id} not found")
    return ok(_to_read(site))


@router.get(
    "/{site_id}/groups",
    summary="List groups in site",
    description=(
        "Return every DeviceGroup in this site the caller can see. Callers "
        "with a site-wide grant see every group; callers with only "
        "group-scoped grants see just those groups. A caller with no grant "
        "on either the site or any of its groups gets 404."
    ),
)
def list_groups_for_site(
    site_id: int,
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.api.device_groups import _to_read as _group_to_read
    from app.composition import device_group_repository, site_repository

    site = site_repository.get(site_id)
    if site is None:
        raise NotFoundError(f"Site {site_id} not found")
    # No pedimos observer-a-nivel-site (rol_para(site_id, None) sólo mira
    # grants site-wide, D11/D25) -- filtramos por visibilidad efectiva
    # via visibles_para_usuario(): sitewide-observer ve todos los grupos,
    # group-observer sólo su(s) grupo(s), el resto 404.
    visible = [g for g in device_group_repository.visibles_para_usuario(scope) if g.site_id == site_id]
    if not visible and not scope.es_system_admin:
        raise NotFoundError(f"Site {site_id} not found")
    member_counts = device_group_repository.contar_miembros_batch([g.id for g in visible])
    return ok([
        _group_to_read(g, member_count=member_counts.get(g.id, 0), site_name=site.name)
        for g in visible
    ])


@router.put(
    "/{site_id}",
    summary="Update site",
    description="Update a site's name or description. Requires admin role on the site.",
)
def update_site(
    site_id: int,
    data: SiteUpdate,
    current_user: dict = Depends(require_scope("edit_site")),
):
    from app.composition import event_dispatcher, site_repository

    changed = data.model_dump(exclude_none=True)
    if not changed:
        raise ValidationError("No fields provided for update")
    site = site_repository.get(site_id)
    if not site:
        raise NotFoundError(f"Site {site_id} not found")
    if data.name is not None and data.name != site.name:
        conflicto = [s for s in site_repository.list(name=data.name) if s.id != site_id]
        if conflicto:
            raise ValidationError(f"Site '{data.name}' already exists")
        site.renombrar(data.name)
    if data.description is not None:
        site.actualizar_descripcion(data.description)
    site = site_repository.add(site)
    event_dispatcher.despachar([DomainEvent(
        "update_site", site, None, current_user["username"],
        {"resource_id": site_id, "site_id": site_id, "updated_fields": changed},
    )])
    return ok(_to_read(site))


@router.delete(
    "/{site_id}",
    summary="Delete site",
    description=(
        "Permanently delete a site. Base-Infrastructure is never deletable. "
        "Returns 409 if the site still owns devices. Requires system-admin."
    ),
)
def delete_site(
    site_id: int,
    current_user: dict = Depends(require_system_admin),
    _elevated: dict = Depends(require_elevated),
):
    from app.composition import event_dispatcher, site_repository

    site = site_repository.get(site_id)
    if site is None:
        raise NotFoundError(f"Site {site_id} not found")
    try:
        deleted = site_repository.eliminar(site_id)
    except SiteHasDevicesError as e:
        raise ConflictError(str(e))
    except ValueError as e:
        # Raised for Base-Infrastructure deletion attempts.
        raise ValidationError(str(e))
    if not deleted:
        raise NotFoundError(f"Site {site_id} not found")
    event_dispatcher.despachar([DomainEvent(
        "delete_site", site, None, current_user["username"],
        {"resource_id": site_id, "site_id": site_id},
    )])
    return ok({"site_id": site_id})


# Re-export for type checkers / explicit imports
__all__ = ["router", "SiteCreate", "SiteRead", "SiteUpdate"]
