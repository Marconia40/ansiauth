import logging
from datetime import datetime, timezone

from app.models.job import Job

logger = logging.getLogger(__name__)

_jobs: dict[str, Job] = {}


def create_job() -> Job:
    job = Job()
    _jobs[job.job_id] = job
    logger.info("Job %s created", job.job_id)
    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def get_all_jobs() -> list[Job]:
    return list(_jobs.values())


def update_job(job_id: str, status: str, result: dict | None = None, error: str | None = None):
    job = _jobs.get(job_id)
    if not job:
        return
    job.status = status
    if result is not None:
        job.result = result
    if error is not None:
        job.error = error
    if status == "running":
        job.started_at = datetime.now(timezone.utc)
        logger.info("Job %s started", job_id)
    elif status == "completed":
        job.finished_at = datetime.now(timezone.utc)
        logger.info("Job %s completed", job_id)
    elif status == "failed":
        job.finished_at = datetime.now(timezone.utc)
        logger.warning("Job %s failed: %s", job_id, error or result)


def cancel_job(job_id: str) -> Job | None:
    job = _jobs.get(job_id)
    if not job:
        return None
    if job.status in ("pending", "running"):
        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc)
        logger.info("Job %s cancelled", job_id)
    return job
