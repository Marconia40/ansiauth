from fastapi import APIRouter, Depends, HTTPException

from app.core.dependencies import get_current_user
from app.services import audit_service, job_service

router = APIRouter()


@router.get("/{job_id}")
def get_job(job_id: str):
    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "result": job.result,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


@router.post("/{job_id}/cancel")
def cancel_job(job_id: str, current_user: dict = Depends(get_current_user)):
    job = job_service.cancel_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
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
    return {"job_id": job.job_id, "status": job.status}
