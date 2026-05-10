from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.dependencies import require_role
from app.services import audit_service

router = APIRouter()


@router.get("/")
def get_audit_log(
    user: Optional[str] = None,
    action: Optional[str] = None,
    resource: Optional[str] = None,
    from_date: Optional[datetime] = Query(default=None),
    to_date: Optional[datetime] = Query(default=None),
    device_id: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    current_user: dict = Depends(require_role("admin")),
):
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(status_code=422, detail="from_date must not be after to_date")
    return audit_service.get_audit_log(
        user=user,
        action=action,
        resource=resource,
        from_date=from_date,
        to_date=to_date,
        device_id=device_id,
        skip=skip,
        limit=limit,
    )
