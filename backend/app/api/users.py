import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import require_role
from app.core.exceptions import NotFoundError, ValidationError
from app.schemas.user import UserCreate, UserUpdate
from app.services import audit_service, user_service

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
def create_user(data: UserCreate, current_user: dict = Depends(require_role("admin"))):
    if data.role == "super-admin" and current_user["role"] != "super-admin":
        raise HTTPException(status_code=403, detail="Only super-admins can create super-admin accounts")
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
    current_user: dict = Depends(require_role("admin")),
    include_inactive: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
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
def get_user(user_id: int, current_user: dict = Depends(require_role("admin"))):
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
def update_user(user_id: int, data: UserUpdate, current_user: dict = Depends(require_role("super-admin"))):
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
def deactivate_user(user_id: int, current_user: dict = Depends(require_role("super-admin"))):
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
