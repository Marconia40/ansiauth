from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.config import AUDIT_RETENTION_DAYS
from app.core.dependencies import require_role
from app.services import audit_service

router = APIRouter()


@router.post(
    "/purge",
    summary="Purge audit log",
    description=(
        "Delete audit records older than `retention_days`. "
        "Defaults to the server-configured retention period. "
        "Parent records referenced by newer child rows are preserved to maintain chain integrity. "
        "Requires super-admin role."
    ),
)
def purge_audit_log(
    retention_days: Optional[int] = Query(default=None, ge=1),
    current_user: dict = Depends(require_role("super-admin")),
):
    days = retention_days if retention_days is not None else AUDIT_RETENTION_DAYS
    deleted = audit_service.purge_old_records(
        retention_days=days,
        triggered_by=current_user["username"],
    )
    return {"success": True, "data": {"deleted": deleted, "retention_days": days}}


@router.get(
    "/",
    summary="List audit log",
    description=(
        "Return append-only audit log entries in reverse chronological order. "
        "Supports filtering by `user`, `action`, `resource`, `status`, `device_id`, and UTC date range. "
        "Paginated via either `skip`/`limit` (raw offsets) or `page`/`page_size`. "
        "Total matching record count is exposed via the `X-Total-Count` response header "
        "and `Access-Control-Expose-Headers`. Requires admin role or higher."
    ),
)
def get_audit_log(
    response: Response,
    user: Optional[str] = None,
    action: Optional[str] = None,
    resource: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[datetime] = Query(default=None),
    to_date: Optional[datetime] = Query(default=None),
    device_id: Optional[str] = Query(default=None),
    site_id: Optional[int] = Query(default=None, ge=1),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
    page: Optional[int] = Query(default=None, ge=1),
    page_size: Optional[int] = Query(default=None, ge=1, le=1000),
    current_user: dict = Depends(require_role("admin")),
):
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(status_code=422, detail="from_date must not be after to_date")

    if page is not None or page_size is not None:
        effective_page_size = page_size if page_size is not None else limit
        effective_page = page if page is not None else 1
        skip = (effective_page - 1) * effective_page_size
        limit = effective_page_size

    total = audit_service.count_audit_log(
        user=user,
        action=action,
        resource=resource,
        status=status,
        from_date=from_date,
        to_date=to_date,
        device_id=device_id,
        site_id=site_id,
    )
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"

    return audit_service.get_audit_log(
        user=user,
        action=action,
        resource=resource,
        status=status,
        from_date=from_date,
        to_date=to_date,
        device_id=device_id,
        site_id=site_id,
        skip=skip,
        limit=limit,
    )
