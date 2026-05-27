import logging

from fastapi import APIRouter, Depends

from app.core.dependencies import require_role
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.schemas.site import SiteCreate, SiteRead, SiteUpdate
from app.services import audit_service, site_service
from app.services.site_service import SiteHasDevicesError

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/",
    summary="Create site",
    description="Create a named site. Names are trimmed and must be unique. Requires admin role.",
)
def create_site(data: SiteCreate, current_user: dict = Depends(require_role("admin"))):
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
    return {"success": True, "data": site.model_dump()}


@router.get(
    "/",
    summary="List sites",
    description="Return all sites with device counts, sorted by name. Requires observer role or higher.",
)
def list_sites(current_user: dict = Depends(require_role("observer"))):
    sites = site_service.list_sites()
    return {"success": True, "data": [s.model_dump() for s in sites]}


@router.get(
    "/{site_id}",
    summary="Get site",
    description="Return a single site by ID. Requires observer role or higher.",
)
def get_site(site_id: int, current_user: dict = Depends(require_role("observer"))):
    site = site_service.get_site(site_id)
    if not site:
        raise NotFoundError(f"Site {site_id} not found")
    return {"success": True, "data": site.model_dump()}


@router.put(
    "/{site_id}",
    summary="Update site",
    description="Update a site's name or description. Requires admin role.",
)
def update_site(site_id: int, data: SiteUpdate, current_user: dict = Depends(require_role("admin"))):
    changed = data.model_dump(exclude_none=True)
    if not changed:
        raise ValidationError("No fields provided for update")
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
        "Permanently delete a site. Returns 409 Conflict if the site still owns devices — "
        "devices are never cascade-deleted. Requires admin role."
    ),
)
def delete_site(site_id: int, current_user: dict = Depends(require_role("admin"))):
    try:
        deleted = site_service.delete_site(site_id)
    except SiteHasDevicesError as e:
        raise ConflictError(str(e))
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
