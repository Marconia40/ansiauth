from datetime import datetime, timezone
from app.models.job import Job

_jobs: dict[str, Job] = {}


def create_job() -> Job:
    job = Job()
    _jobs[job.job_id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def update_job(job_id: str, status: str, result: dict | None = None):
    job = _jobs.get(job_id)
    if job:
        job.status = status
        job.result = result
        if status == "running":
            job.started_at = datetime.now(timezone.utc)
        elif status in ("completed", "failed"):
            job.finished_at = datetime.now(timezone.utc)


def cancel_job(job_id: str) -> Job | None:
    job = _jobs.get(job_id)
    if not job:
        return None
    if job.status in ("pending", "running"):
        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc)
    return job
