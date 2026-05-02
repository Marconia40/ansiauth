from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import require_role
from app.services import audit_service

router = APIRouter()


@router.get("/")
def get_audit_log(
    user: Optional[str] = None,
    action: Optional[str] = None,
    resource: Optional[str] = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: dict = Depends(require_role("admin")),
):
    return audit_service.get_audit_log(
        user=user,
        action=action,
        resource=resource,
        skip=skip,
        limit=limit,
    )
