import logging

from fastapi import APIRouter, Depends

from app.core.exceptions import ConflictError, NotFoundError, SiteHasDevicesError, ValidationError
from app.core.response import ok
from app.core.scope import (
    obtener_scope,
    require_authenticated,
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


def _to_read(site) -> dict:
    from app.composition import site_repository

    return SiteRead(
        id=site.id, name=site.name, description=site.description,
        created_at=site.created_at, updated_at=site.updated_at,
        device_count=site_repository.contar_devices(site.id),
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
    return ok([_to_read(s) for s in sites])


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
    description="Return every DeviceGroup that belongs to this site.",
)
def list_groups_for_site(
    site_id: int,
    current_user: dict = Depends(require_scope("list_site_groups")),
):
    from app.api.device_groups import _to_read as _group_to_read
    from app.composition import device_group_repository, site_repository

    site = site_repository.get(site_id)
    if site is None:
        raise NotFoundError(f"Site {site_id} not found")
    groups = device_group_repository.en_site(site_id)
    return ok([_group_to_read(g) for g in groups])


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
def delete_site(site_id: int, current_user: dict = Depends(require_system_admin)):
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
