import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_current_user
from app.core.exceptions import NotFoundError
from app.services import audit_service, job_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _format_job(job) -> dict:
    return {
        "job_id": job.job_id,
        "status": job.status,
        "error": job.error,
        "result": job.result,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@router.get("/")
def list_jobs(current_user: dict = Depends(get_current_user)):
    return {"success": True, "data": [_format_job(j) for j in job_service.get_all_jobs()]}


@router.get("/{job_id}")
def get_job(job_id: str):
    job = job_service.get_job(job_id)
    if not job:
        raise NotFoundError(f"Job '{job_id}' not found")
    return {"success": True, "data": _format_job(job)}


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, current_user: dict = Depends(get_current_user)):
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
