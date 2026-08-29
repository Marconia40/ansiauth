import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.scope import obtener_scope, require_authenticated, require_scope
from app.db.models import SiteModel
from app.db.session import get_session
from app.models.visibility_scope import VisibilityScope
from app.schemas.site import SiteCreate, SiteRead, SiteUpdate
from app.services import audit_service, site_service
from app.services.site_service import (
    BASE_INFRA_SITE_KIND,
    DEFAULT_GROUP_NAME,
    SiteHasDevicesError,
)

logger = logging.getLogger(__name__)
router = APIRouter()


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
    current_user: dict = Depends(require_authenticated),
):
    if not current_user.get("is_system_admin"):
        raise HTTPException(
            status_code=403,
            detail="create_site requires system-admin",
        )
    try:
        site = site_service.create_site(name=data.name, description=data.description)
    except ValueError as e:
        raise ValidationError(str(e))
    audit_service.log_action(
        user=current_user["username"],
        action="create_site",
        resource="site",
        resource_id=str(site.id),
        details={"name": site.name},
    )
    # Second audit row for the Default group creation — the site + group are
    # one atomic operation but the audit surface makes both transitions
    # visible.
    if site.id is not None:
        audit_service.log_action(
            user=current_user["username"],
            action="create_device_group",
            resource="device_group",
            details={
                "name": DEFAULT_GROUP_NAME,
                "site_id": site.id,
                "is_default": True,
                "reason": "site_bootstrap",
            },
        )
    return {"success": True, "data": site.model_dump()}


@router.get(
    "/",
    summary="List sites",
    description=(
        "Return sites visible to the caller. Reads from role_assignments and "
        "hides Base-Infrastructure from non system-admins (D14)."
    ),
)
def list_sites(current_user: dict = Depends(require_authenticated)):
    sites = site_service.list_sites_for_user(current_user)
    return {"success": True, "data": [s.model_dump() for s in sites]}


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
    site = site_service.get_site(site_id)
    if not site:
        raise NotFoundError(f"Site {site_id} not found")
    role = scope.rol_para(site_id, None)
    with get_session() as session:
        kind = (
            session.query(SiteModel.kind).filter(SiteModel.id == site_id).scalar()
        )
    # D14: Base-Infrastructure is invisible to non system-admins even if a
    # stray grant somehow lands on it.
    if kind == BASE_INFRA_SITE_KIND and not current_user.get("is_system_admin"):
        raise NotFoundError(f"Site {site_id} not found")
    if role is None:
        raise NotFoundError(f"Site {site_id} not found")
    return {"success": True, "data": site.model_dump()}


@router.get(
    "/{site_id}/groups",
    summary="List groups in site",
    description="Return every DeviceGroup that belongs to this site.",
)
def list_groups_for_site(
    site_id: int,
    current_user: dict = Depends(require_scope("list_site_groups")),
):
    from app.services import device_group_service
    groups = device_group_service.list_site_groups(site_id)
    if groups is None:
        raise NotFoundError(f"Site {site_id} not found")
    return {"success": True, "data": [g.model_dump() for g in groups]}


@router.put(
    "/{site_id}",
    summary="Update site",
    description="Update a site's name or description. Requires admin role on the site.",
)
def update_site(
    site_id: int,
    data: SiteUpdate,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    changed = data.model_dump(exclude_none=True)
    if not changed:
        raise ValidationError("No fields provided for update")
    role = scope.rol_para(site_id, None)
    if not current_user.get("is_system_admin") and role != "admin":
        raise HTTPException(
            status_code=403,
            detail=f"edit site requires admin on site {site_id} (got {role or 'none'})",
        )
    try:
        site = site_service.update_site(
            site_id=site_id,
            name=data.name,
            description=data.description,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    if not site:
        raise NotFoundError(f"Site {site_id} not found")
    audit_service.log_action(
        user=current_user["username"],
        action="update_site",
        resource="site",
        resource_id=str(site_id),
        details={"site_id": site_id, "updated_fields": changed},
    )
    return {"success": True, "data": site.model_dump()}


@router.delete(
    "/{site_id}",
    summary="Delete site",
    description=(
        "Permanently delete a site. Base-Infrastructure is never deletable. "
        "Returns 409 if the site still owns devices. Requires system-admin."
    ),
)
def delete_site(site_id: int, current_user: dict = Depends(require_authenticated)):
    if not current_user.get("is_system_admin"):
        raise HTTPException(
            status_code=403,
            detail="delete_site requires system-admin",
        )
    try:
        deleted = site_service.delete_site(site_id)
    except SiteHasDevicesError as e:
        raise ConflictError(str(e))
    except ValueError as e:
        # Raised for Base-Infrastructure deletion attempts.
        raise ValidationError(str(e))
    if not deleted:
        raise NotFoundError(f"Site {site_id} not found")
    audit_service.log_action(
        user=current_user["username"],
        action="delete_site",
        resource="site",
        resource_id=str(site_id),
        details={"site_id": site_id},
    )
    return {"success": True, "data": {"site_id": site_id}}


# Re-export for type checkers / explicit imports
__all__ = ["router", "SiteCreate", "SiteRead", "SiteUpdate"]
