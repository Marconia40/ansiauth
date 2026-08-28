import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.exceptions import NotFoundError
from app.core.scope import require_authenticated
from app.services import audit_service, job_service

logger = logging.getLogger(__name__)
router = APIRouter()

_VALID_STATUSES = frozenset({"pending", "running", "completed", "failed", "cancelled"})


def _format_job(job) -> dict:
    started = _ensure_aware(job.started_at)
    finished = _ensure_aware(job.finished_at)
    duration_ms = (
        round((finished - started).total_seconds() * 1000)
        if started and finished else None
    )
    return {
        "job_id": job.job_id,
        "status": job.status,
        "playbook": job.playbook,
        "device": job.device,
        "error": job.error,
        "result": job.result,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "retry_count": job.retry_count,
        "max_retries": job.max_retries,
        "rollback_performed": job.rollback_performed,
        "rollback_success": job.rollback_success,
        "pre_state": job.pre_state,
        "last_error": job.last_error,
        "current_step": job.current_step,
        "group_job_id": job.group_job_id,
        "execution_summary": {
            "attempts": job.retry_count + 1 if job.status in ("completed", "failed") else None,
            "rollback_performed": job.rollback_performed,
            "rollback_success": job.rollback_success,
            "duration_ms": duration_ms,
        },
    }


def _ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get(
    "/",
    summary="List jobs",
    description=(
        "List background Ansible execution jobs with optional filters. "
        "Supports filtering by status (`pending`, `running`, `completed`, `failed`, `cancelled`), "
        "device, and date range. Paginated — defaults to 50 per page. "
        "Accessible to all authenticated users."
    ),
)
def list_jobs(
    current_user: dict = Depends(require_authenticated),
    status: Optional[str] = Query(default=None),
    device_id: Optional[str] = Query(default=None),
    site_id: Optional[int] = Query(default=None, ge=1),
    from_date: Optional[datetime] = Query(default=None),
    to_date: Optional[datetime] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    if status is not None and status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(_VALID_STATUSES)}",
        )
    from_date = _ensure_aware(from_date)
    to_date = _ensure_aware(to_date)
    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(status_code=422, detail="from_date must not be after to_date")

    # Scope-aware visible device set from role_assignments (system-admins see
    # everything → None sentinel).
    from app.services.inventory_service import Inventory
    allowed = Inventory()._visible_device_names(
        current_user, site_id=site_id, device_group_id=None,
    )

    jobs, total = job_service.query_jobs(
        status=status,
        device=device_id,
        from_date=from_date,
        to_date=to_date,
        site_id=site_id,
        allowed_devices=allowed,
        page=page,
        page_size=page_size,
    )
    return {
        "success": True,
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_format_job(j) for j in jobs],
    }


@router.get(
    "/{job_id}",
    summary="Get job",
    description="Return the full status and result of a single background job by its UUID. Accessible to all authenticated users.",
)
def get_job(job_id: str, current_user: dict = Depends(require_authenticated)):
    job = job_service.get_job(job_id)
    if not job:
        raise NotFoundError(f"Job '{job_id}' not found")
    if job.device:
        _check_device_scope(current_user, job.device, min_role="observer")
    return {"success": True, "data": _format_job(job)}


_LVL = {"observer": 1, "operator": 2, "admin": 3, "super-admin": 99}


def _check_device_scope(user: dict, device_name: str, *, min_role: str) -> None:
    """Shared authz for job endpoints — resolves the caller's effective role
    on the target device and rejects with 403 if it is below ``min_role``."""
    from app.services.effective_role import effective_role
    from app.db.session import get_session
    with get_session() as session:
        role = effective_role(session, user, "device", device_name)
    if _LVL.get(role or "", 0) < _LVL[min_role]:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Job operation on device '{device_name}' requires "
                f"role >= {min_role} (got {role or 'none'})"
            ),
        )


@router.post(
    "/{job_id}/cancel",
    summary="Cancel job",
    description=(
        "Request cancellation of a pending job. "
        "Jobs that have already started cannot be cancelled — a 409 is returned in that case. "
        "Accessible to all authenticated users."
    ),
)
def cancel_job(job_id: str, current_user: dict = Depends(require_authenticated)):
    # Look up first so we can authz before mutating state.
    existing = job_service.get_job(job_id)
    if not existing:
        raise NotFoundError(f"Job '{job_id}' not found")
    if existing.device:
        _check_device_scope(current_user, existing.device, min_role="operator")
    job = job_service.cancel_job(job_id)
    if not job:
        raise NotFoundError(f"Job '{job_id}' not found")
    if job.status != "cancelled":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot cancel job with status '{job.status}'"
        )
    audit_service.log_action(
        user=current_user["username"],
        action="cancel_job",
        resource="job",
        details={"job_id": job_id},
        job_id=job_id,
    )
    return {"success": True, "data": {"job_id": job.job_id, "status": job.status}}
