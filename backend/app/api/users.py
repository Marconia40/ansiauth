import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import require_role
from app.core.exceptions import NotFoundError, ValidationError
from app.schemas.user import UserCreate, UserUpdate
from app.services import audit_service, user_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/")
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


@router.get("/")
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


@router.get("/{user_id}")
def get_user(user_id: int, current_user: dict = Depends(require_role("admin"))):
    user = user_service.get_by_id(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} not found")
    return {"success": True, "data": user.model_dump()}


@router.put("/{user_id}")
def update_user(user_id: int, data: UserUpdate, current_user: dict = Depends(require_role("super-admin"))):
    try:
        user = user_service.update_user(user_id, data)
    except ValueError as e:
        raise ValidationError(str(e))
    audit_service.log_action(
        user=current_user["username"],
        action="update_user",
        resource="user",
        resource_id=str(user_id),
        details={"updated_fields": data.model_dump(exclude_none=True)},
    )
    return {"success": True, "data": user.model_dump()}


@router.delete("/{user_id}")
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
