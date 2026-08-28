import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.config import settings
from app.core.dependencies import require_role
from app.core.exceptions import NotFoundError, ValidationError
from app.core.scope import (
    require_authenticated,
    require_system_admin,
)
from app.schemas.role_assignment import (
    RoleAssignmentCreate,
    SystemAdminUpdate,
)
from app.schemas.user import UserCreate, UserUpdate
from app.services import audit_service, user_service
from app.services.role_assignment_service import RoleAssignmentService

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/",
    summary="Create user",
    description=(
        "Create a new user account. Admin role is required; "
        "only super-admins may create super-admin accounts. "
        "Passwords are hashed with PBKDF2-SHA256 and never returned in responses."
    ),
)
def create_user(data: UserCreate, current_user: dict = Depends(require_authenticated)):
    if settings.MSP_STRICT_HIERARCHY:
        # Under MSP-strict: only system-admins may create users.
        if not current_user.get("is_system_admin"):
            raise HTTPException(
                status_code=403,
                detail="create_user requires system-admin under MSP-strict",
            )
    else:
        if current_user["role"] not in {"admin", "super-admin"}:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        if data.role == "super-admin" and current_user["role"] != "super-admin":
            raise HTTPException(
                status_code=403,
                detail="Only super-admins can create super-admin accounts",
            )
    try:
        user = user_service.create_user(data)
    except ValueError as e:
        raise ValidationError(str(e))
    audit_service.log_action(
        user=current_user["username"],
        action="create_user",
        resource="user",
        resource_id=str(user.id),
        details={"username": user.username, "role": user.role},
    )
    return {"success": True, "data": user.model_dump()}


@router.get(
    "/",
    summary="List users",
    description=(
        "Return all user accounts. Active users only by default; "
        "pass `include_inactive=true` to include deactivated accounts. "
        "Paginated — defaults to 50 per page. Requires admin role or higher."
    ),
)
def list_users(
    current_user: dict = Depends(require_authenticated),
    include_inactive: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    if settings.MSP_STRICT_HIERARCHY:
        if not current_user.get("is_system_admin"):
            raise HTTPException(
                status_code=403,
                detail="list_users requires system-admin under MSP-strict",
            )
    else:
        if current_user["role"] not in {"admin", "super-admin"}:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    all_users = user_service.list_users(include_inactive=include_inactive)
    total = len(all_users)
    start = (page - 1) * page_size
    return {
        "success": True,
        "data": [u.model_dump() for u in all_users[start : start + page_size]],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get(
    "/{user_id}",
    summary="Get user",
    description="Return a single user by numeric ID. Requires admin role or higher.",
)
def get_user(user_id: int, current_user: dict = Depends(require_authenticated)):
    if settings.MSP_STRICT_HIERARCHY:
        # Callers may always fetch themselves; otherwise system-admin.
        if not current_user.get("is_system_admin") and current_user.get("id") != user_id:
            raise HTTPException(
                status_code=403,
                detail="get_user requires system-admin or self",
            )
    else:
        if current_user["role"] not in {"admin", "super-admin"}:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    user = user_service.get_by_id(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    return {"success": True, "data": user.model_dump()}


@router.put(
    "/{user_id}",
    summary="Update user",
    description=(
        "Update user fields: email, role, password, or active status. "
        "All fields are optional. The last active admin and last active super-admin "
        "cannot be deactivated via this endpoint. Requires super-admin role."
    ),
)
def update_user(
    user_id: int,
    data: UserUpdate,
    current_user: dict = Depends(require_authenticated),
):
    # D26: split — activation/deactivation and password/email edits live at
    # the system-admin gate; profile self-edit shipped later. Phase 3 keeps
    # the endpoint at system-admin/super-admin only.
    if settings.MSP_STRICT_HIERARCHY:
        if not current_user.get("is_system_admin"):
            raise HTTPException(
                status_code=403,
                detail="update_user requires system-admin under MSP-strict",
            )
    else:
        if current_user["role"] != "super-admin":
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        user = user_service.update_user(user_id, data)
    except ValueError as e:
        raise ValidationError(str(e))
    audit_fields = {k: v for k, v in data.model_dump(exclude_none=True).items() if k != "password"}
    if data.password is not None:
        audit_fields["password_changed"] = True
    audit_service.log_action(
        user=current_user["username"],
        action="update_user",
        resource="user",
        resource_id=str(user_id),
        details={"updated_fields": audit_fields},
    )
    return {"success": True, "data": user.model_dump()}


@router.delete(
    "/{user_id}",
    summary="Deactivate user",
    description=(
        "Soft-delete a user account. The last active admin or super-admin cannot be deactivated. "
        "The user record is retained for audit purposes. Requires super-admin role."
    ),
)
def deactivate_user(user_id: int, current_user: dict = Depends(require_authenticated)):
    if settings.MSP_STRICT_HIERARCHY:
        if not current_user.get("is_system_admin"):
            raise HTTPException(
                status_code=403,
                detail="deactivate_user requires system-admin under MSP-strict",
            )
    else:
        if current_user["role"] != "super-admin":
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        user = user_service.deactivate_user(user_id)
    except ValueError as e:
        raise ValidationError(str(e))
    audit_service.log_action(
        user=current_user["username"],
        action="deactivate_user",
        resource="user",
        resource_id=str(user_id),
        details={"username": user.username},
    )
    return {"success": True, "data": {"id": user_id, "is_active": False}}


# ─── MSP: Phase 3 — grants / system-admin ───────────────────────────────────

@router.post(
    "/{user_id}/grants",
    summary="Grant role assignment",
    description=(
        "Grant a role at (site) or (site, group) scope. Under MSP-strict, only "
        "system-admins or site-admins may issue grants (group-admins may not "
        "delegate — D25)."
    ),
    status_code=201,
)
def create_grant(
    user_id: int,
    body: RoleAssignmentCreate,
    current_user: dict = Depends(require_authenticated),
):
    try:
        record = RoleAssignmentService().grant(
            target_user_id=user_id,
            site_id=body.site_id,
            device_group_id=body.device_group_id,
            role=body.role,
            actor=current_user,
        )
    except ValueError as exc:
        raise ValidationError(str(exc))
    return {"success": True, "data": record.model_dump()}


@router.get(
    "/{user_id}/grants",
    summary="List role assignments for user",
    description=(
        "Return the target user's grants filtered by what the viewer may see: "
        "system-admins and the target themselves see everything; other admins "
        "see only grants at sites where they hold an admin grant."
    ),
)
def list_grants(user_id: int, current_user: dict = Depends(require_authenticated)):
    records = RoleAssignmentService().list_for_user(user_id, viewer=current_user)
    return {"success": True, "data": [r.model_dump() for r in records]}


@router.delete(
    "/{user_id}/grants/{grant_id}",
    summary="Revoke role assignment",
    description="Delete a specific grant. Same authz as POST.",
    status_code=204,
)
def delete_grant(
    user_id: int,
    grant_id: int,
    current_user: dict = Depends(require_authenticated),
):
    RoleAssignmentService().revoke(grant_id, actor=current_user)
    return Response(status_code=204)


@router.put(
    "/{user_id}/system-admin",
    summary="Toggle system-admin flag",
    description=(
        "Promote or demote a user to/from system-admin. The last active "
        "system-admin cannot be demoted. Requires system-admin."
    ),
)
def set_system_admin(
    user_id: int,
    body: SystemAdminUpdate,
    current_user: dict = Depends(require_system_admin),
):
    RoleAssignmentService().set_system_admin(
        target_user_id=user_id,
        is_system_admin=body.is_system_admin,
        actor=current_user,
    )
    return {
        "success": True,
        "data": {"id": user_id, "is_system_admin": body.is_system_admin},
    }


# ─── T3.3b — compat shim for legacy PUT /allowed-sites ─────────────────────

class _AllowedSitesBody(dict):
    """Minimal shim so callers using the legacy payload still validate."""


from pydantic import BaseModel


class AllowedSitesUpdate(BaseModel):
    allowed_site_ids: list[int]


@router.put(
    "/{user_id}/allowed-sites",
    summary="[Deprecated] Replace legacy allowed_sites",
    description=(
        "**Deprecated (T3.3b).** Rewrites the request as a batch of observer "
        "site-wide grants: revokes every existing observer site-wide grant for "
        "this user and creates one per site_id in the body. Non-observer grants "
        "and group-scoped grants are untouched. Removed in Phase 5."
    ),
    deprecated=True,
)
def update_allowed_sites(
    user_id: int,
    body: AllowedSitesUpdate,
    response: Response,
    current_user: dict = Depends(require_authenticated),
):
    if settings.MSP_STRICT_HIERARCHY:
        if not current_user.get("is_system_admin"):
            raise HTTPException(
                status_code=403,
                detail="allowed-sites shim requires system-admin under MSP-strict",
            )
    else:
        if current_user["role"] not in {"admin", "super-admin"}:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    # Delegate to RoleAssignmentService so audit + invariants apply uniformly.
    from app.db.models import RoleAssignmentModel
    from app.db.session import get_session
    svc = RoleAssignmentService()
    with get_session() as session:
        existing_observer = (
            session.query(RoleAssignmentModel.id, RoleAssignmentModel.site_id)
            .filter(
                RoleAssignmentModel.user_id == user_id,
                RoleAssignmentModel.device_group_id.is_(None),
                RoleAssignmentModel.role == "observer",
            )
            .all()
        )
        to_revoke = [gid for (gid, _sid) in existing_observer]
    for gid in to_revoke:
        try:
            svc.revoke(gid, actor=current_user)
        except HTTPException:
            # If the actor cannot revoke a stray grant (edge case: system-admin
            # was demoted mid-request), surface a clean 403.
            raise
    created = []
    for sid in sorted(set(body.allowed_site_ids)):
        record = svc.grant(
            target_user_id=user_id,
            site_id=sid,
            device_group_id=None,
            role="observer",
            actor=current_user,
        )
        created.append(record.model_dump())
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "Phase-5"
    audit_service.log_action(
        user=current_user["username"],
        action="update_allowed_sites_shim",
        resource="user",
        resource_id=str(user_id),
        details={
            "revoked_observer_grants": len(to_revoke),
            "granted_observer_sites": [g["site_id"] for g in created],
        },
    )
    return {
        "success": True,
        "data": {"grants": created},
    }
