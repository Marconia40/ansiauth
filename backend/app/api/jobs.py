from fastapi import APIRouter, HTTPException
from app.services import job_service

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
    }
