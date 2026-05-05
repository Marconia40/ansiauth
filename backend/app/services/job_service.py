import logging
from datetime import datetime, timezone

from app.models.job import Job

logger = logging.getLogger(__name__)

_jobs: dict[str, Job] = {}


def create_job(
    playbook: str | None = None,
    device: str | None = None,
    parameters: dict | None = None,
    max_retries: int = 3,
) -> Job:
    job = Job(playbook=playbook, device=device, parameters=parameters, max_retries=max_retries)
    _jobs[job.job_id] = job
    logger.info("Job %s created (playbook=%s device=%s)", job.job_id, playbook, device)
    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def get_all_jobs() -> list[Job]:
    return list(_jobs.values())


def update_job(
    job_id: str,
    status: str | None = None,
    result: dict | None = None,
    error: str | None = None,
    retry_count: int | None = None,
    rollback_performed: bool | None = None,
    pre_state: dict | None = None,
    last_error: str | None = None,
    current_step: str | None = None,
) -> None:
    job = _jobs.get(job_id)
    if not job:
        return
    if status is not None:
        job.status = status
    if result is not None:
        job.result = result
    if error is not None:
        job.error = error
    if retry_count is not None:
        job.retry_count = retry_count
    if rollback_performed is not None:
        job.rollback_performed = rollback_performed
    if pre_state is not None:
        job.pre_state = pre_state
    if last_error is not None:
        job.last_error = last_error
    if current_step is not None:
        job.current_step = current_step
    if status == "running":
        job.started_at = datetime.now(timezone.utc)
        logger.info("Job %s started", job_id)
    elif status == "completed":
        job.finished_at = datetime.now(timezone.utc)
        job.current_step = "completed"
        duration = (job.finished_at - job.started_at).total_seconds() if job.started_at else 0.0
        logger.info("Job %s completed in %.2fs (retries=%d)", job_id, duration, job.retry_count)
    elif status == "failed":
        job.finished_at = datetime.now(timezone.utc)
        job.current_step = "failed"
        duration = (job.finished_at - job.started_at).total_seconds() if job.started_at else 0.0
        logger.warning(
            "Job %s failed after %.2fs (retries=%d rollback=%s): %s",
            job_id, duration, job.retry_count, job.rollback_performed, error or result,
        )


def ensure_final_state(job_id: str) -> None:
    """Force any non-terminal job to failed. Called in finally blocks to prevent stuck states."""
    job = _jobs.get(job_id)
    if job and job.status not in ("completed", "failed", "cancelled"):
        logger.warning("Job %s stuck in '%s' — forcing to failed", job_id, job.status)
        job.status = "failed"
        if not job.error:
            job.error = "Unexpected termination"
        job.finished_at = datetime.now(timezone.utc)


def cancel_job(job_id: str) -> Job | None:
    job = _jobs.get(job_id)
    if not job:
        return None
    if job.status in ("pending", "running"):
        job.status = "cancelled"
        job.finished_at = datetime.now(timezone.utc)
        logger.info("Job %s cancelled", job_id)
    return job
