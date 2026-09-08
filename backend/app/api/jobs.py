import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import NotFoundError, ValidationError
from app.core.response import ok
from app.core.scope import authorize_device, obtener_scope, require_authenticated
from app.models.audit import AuditRecord
from app.models.visibility_scope import VisibilityScope

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
        "operation": job.operation,
        "parameters_summary": job.parameters_summary,
        "playbook": job.playbook,
        "device": job.device,
        "error": job.error,
        "error_summary": job.error_summary,
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
    scope: VisibilityScope = Depends(obtener_scope),
    status: Optional[str] = Query(default=None),
    device_id: Optional[str] = Query(default=None),
    site_id: Optional[int] = Query(default=None, ge=1),
    from_date: Optional[datetime] = Query(default=None),
    to_date: Optional[datetime] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
):
    if status is not None and status not in _VALID_STATUSES:
        raise ValidationError(f"status must be one of {sorted(_VALID_STATUSES)}")
    from_date = _ensure_aware(from_date)
    to_date = _ensure_aware(to_date)
    if from_date is not None and to_date is not None and from_date > to_date:
        raise ValidationError("from_date must not be after to_date")

    from app.composition import job_repository

    jobs, total = job_repository.query(
        status=status,
        device=device_id,
        site_id=site_id,
        from_date=from_date,
        to_date=to_date,
        scope=scope,
        page=page,
        page_size=page_size,
    )
    return ok({"total": total, "page": page, "page_size": page_size, "items": [_format_job(j) for j in jobs]})


@router.get(
    "/{job_id}",
    summary="Get job",
    description="Return the full status and result of a single background job by its UUID. Accessible to all authenticated users.",
)
def get_job(
    job_id: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import job_repository

    job = job_repository.get(job_id)
    if not job:
        raise NotFoundError(f"Job '{job_id}' not found")
    if job.device:
        _check_device_scope(scope, job.device, min_role="observer")
    return ok(_format_job(job))


def _check_device_scope(
    scope: VisibilityScope, device_name: str, *, min_role: str,
) -> None:
    """Shared authz for job endpoints — the target device is only known
    after job_repository.get() runs inside the handler, so it can't use
    require_scope()'s pre-handler resolution. Thin wrapper around
    core.scope.authorize_device() (single implementation shared with
    api/vlans.py/api/ports.py, replacing 3 independent copies)."""
    authorize_device(scope, device_name, "job_device_op", min_role)


@router.post(
    "/{job_id}/cancel",
    summary="Cancel job",
    description=(
        "Request cancellation of a pending job. "
        "Jobs that have already started cannot be cancelled — a 409 is returned in that case. "
        "Accessible to all authenticated users."
    ),
)
def cancel_job(
    job_id: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import audit_repository, job_repository

    # Look up first so we can authz before mutating state.
    job = job_repository.get(job_id)
    if job is None:
        raise NotFoundError(f"Job '{job_id}' not found")
    if job.device:
        _check_device_scope(scope, job.device, min_role="operator")
    job.cancelar()
    job_repository.add(job)
    # audit_service.log_action() escrito acá antes -- reemplazado por
    # AuditRepository.append() directo (Fase 3/B1), no AuditRecord.desde()
    # (esa fábrica arma el record a partir de un DomainEvent de
    # Orquestador; cancelar un job no pasa por ahí, no hay VLAN/Puerto
    # ni device.driver involucrado).
    #
    # ``device=job.device`` -- corrección real encontrada probando el
    # filtro de scope de punta a punta (`AuditRepository._aplicar_scope()`,
    # Fase 3): sin `device`, el record queda invisible para cualquier
    # scope no-system-admin -- `_aplicar_scope()` solo deja pasar filas con
    # `device=None` cuando `resource == "auth"`; con `resource="job"` y
    # `device=None` no matchea ninguna de sus condiciones OR, el operador
    # dueño del device nunca veía su propio cancel_job en el audit log.
    audit_repository.append(AuditRecord(
        user=current_user["username"],
        action="cancel_job",
        resource="job",
        resource_id=job_id,
        details={"job_id": job_id},
        job_id=job_id,
        device=job.device,
    ))
    return ok({"job_id": job.job_id, "status": job.status})
