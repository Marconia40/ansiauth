from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.core.config import AUDIT_RETENTION_DAYS
from app.core.scope import obtener_scope, require_authenticated, require_system_admin
from app.models.visibility_scope import VisibilityScope

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
    current_user: dict = Depends(require_system_admin),
):
    from app.composition import audit_repository

    days = retention_days if retention_days is not None else AUDIT_RETENTION_DAYS
    deleted = audit_repository.purge_old(days, triggered_by=current_user["username"])
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
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    # Endpoint stays behind require_authenticated: the D27 scoping filter
    # (AuditRepository.query()'s scope param, Fase 3) *is* the authorization.
    # Non-admins that hit /audit see only rows their grants cover.
    from app.composition import audit_repository

    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(status_code=422, detail="from_date must not be after to_date")

    # Acepta 2 formatos de paginación (skip/limit y page/page_size) -- mismo
    # cálculo real que el endpoint viejo, AuditRepository.query() solo habla
    # page/page_size (Fase 3).
    if page is not None or page_size is not None:
        effective_page = page if page is not None else 1
        effective_page_size = page_size if page_size is not None else limit
    else:
        effective_page = (skip // limit) + 1 if limit else 1
        effective_page_size = limit

    records, total = audit_repository.query(
        user=user, action=action, resource=resource, status=status,
        from_date=from_date, to_date=to_date, device_id=device_id, site_id=site_id,
        scope=scope, page=effective_page, page_size=effective_page_size,
    )
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return records
