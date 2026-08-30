import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.exceptions import NotFoundError, ValidationError
from app.core.scope import (
    require_authenticated,
    require_system_admin,
)
from app.schemas.role_assignment import (
    RoleAssignmentCreate,
    SystemAdminUpdate,
)
from app.models.audit import AuditRecord
from app.models.user import User
from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.services.role_assignment_service import RoleAssignmentService

logger = logging.getLogger(__name__)
router = APIRouter()


def _to_read(user: User) -> dict:
    return UserRead(
        id=user.id, username=user.username, email=user.email,
        is_active=user.is_active, is_system_admin=user.is_system_admin,
        created_at=user.created_at, updated_at=user.updated_at,
    ).model_dump()


@router.post(
    "/",
    summary="Create user",
    description=(
        "Create a new user account. Requires system-admin. "
        "Passwords are hashed with PBKDF2-SHA256 and never returned in responses."
    ),
)
def create_user(data: UserCreate, current_user: dict = Depends(require_authenticated)):
    if not current_user.get("is_system_admin"):
        raise HTTPException(
            status_code=403,
            detail="create_user requires system-admin",
        )
    from app.composition import audit_repository, user_repository

    try:
        user = user_repository.crear(
            username=data.username, password=data.password,
            email=data.email, is_system_admin=data.is_system_admin,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    audit_repository.append(AuditRecord(
        user=current_user["username"],
        action="create_user",
        resource="user",
        resource_id=str(user.id),
        details={"username": user.username},
    ))
    return {"success": True, "data": _to_read(user)}


@router.get(
    "/",
    summary="List users",
    description=(
        "Return all user accounts. Active users only by default; "
        "pass `include_inactive=true` to include deactivated accounts. "
        "Paginated — defaults to 50 per page. Requires system-admin."
    ),
)
def list_users(
    current_user: dict = Depends(require_authenticated),
    include_inactive: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    if not current_user.get("is_system_admin"):
        raise HTTPException(
            status_code=403,
            detail="list_users requires system-admin",
        )
    from app.composition import user_repository

    all_users = user_repository.listar(incluir_inactivos=include_inactive)
    total = len(all_users)
    start = (page - 1) * page_size
    return {
        "success": True,
        "data": [_to_read(u) for u in all_users[start : start + page_size]],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get(
    "/{user_id}",
    summary="Get user",
    description="Return a single user by numeric ID. Requires system-admin or self.",
)
def get_user(user_id: int, current_user: dict = Depends(require_authenticated)):
    if not current_user.get("is_system_admin") and current_user.get("id") != user_id:
        raise HTTPException(
            status_code=403,
            detail="get_user requires system-admin or self",
        )
    from app.composition import user_repository

    user = user_repository.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    return {"success": True, "data": _to_read(user)}


@router.put(
    "/{user_id}",
    summary="Update user",
    description=(
        "Update user fields: email, password, or active status. "
        "All fields are optional. The last active system-admin cannot be "
        "deactivated via this endpoint. Requires system-admin."
    ),
)
def update_user(
    user_id: int,
    data: UserUpdate,
    current_user: dict = Depends(require_authenticated),
):
    if not current_user.get("is_system_admin"):
        raise HTTPException(
            status_code=403,
            detail="update_user requires system-admin",
        )
    from app.composition import audit_repository, user_repository

    user = user_repository.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    try:
        if data.email is not None:
            email_norm = data.email.lower()
            clash = [u for u in user_repository.listar(incluir_inactivos=True) if u.email == email_norm]
            if clash and clash[0].id != user_id:
                raise ValidationError(f"Email '{email_norm}' is already registered")
            user.actualizar(email=data.email)
        if data.password is not None:
            user.actualizar(password=data.password)
        if data.is_active is False:
            if user_repository.es_ultimo_admin_activo(user_id):
                raise ValidationError("Cannot deactivate the last active system-admin account")
            user.desactivar()
        elif data.is_active is True:
            user.activar()
    except ValueError as e:
        raise ValidationError(str(e))
    user = user_repository.add(user)
    audit_fields = {k: v for k, v in data.model_dump(exclude_none=True).items() if k != "password"}
    if data.password is not None:
        audit_fields["password_changed"] = True
    audit_repository.append(AuditRecord(
        user=current_user["username"],
        action="update_user",
        resource="user",
        resource_id=str(user_id),
        details={"updated_fields": audit_fields},
    ))
    return {"success": True, "data": _to_read(user)}


@router.delete(
    "/{user_id}",
    summary="Deactivate user",
    description=(
        "Soft-delete a user account. The last active system-admin cannot be "
        "deactivated. The user record is retained for audit purposes. Requires "
        "system-admin."
    ),
)
def deactivate_user(user_id: int, current_user: dict = Depends(require_authenticated)):
    if not current_user.get("is_system_admin"):
        raise HTTPException(
            status_code=403,
            detail="deactivate_user requires system-admin",
        )
    from app.composition import audit_repository, user_repository

    user = user_repository.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    if user_repository.es_ultimo_admin_activo(user_id):
        raise ValidationError("Cannot deactivate the last active system-admin account")
    user.desactivar()
    user_repository.add(user)
    audit_repository.append(AuditRecord(
        user=current_user["username"],
        action="deactivate_user",
        resource="user",
        resource_id=str(user_id),
        details={"username": user.username},
    ))
    return {"success": True, "data": {"id": user_id, "is_active": False}}


# ─── MSP: grants / system-admin ─────────────────────────────────────────────


@router.post(
    "/{user_id}/grants",
    summary="Grant role assignment",
    description=(
        "Grant a role at (site) or (site, group) scope. Only system-admins or "
        "site-admins may issue grants (group-admins may not delegate — D25)."
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
