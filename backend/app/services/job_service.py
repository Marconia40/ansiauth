import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.db.models import JobModel
from app.db.session import get_session
from app.models.job import Job

logger = logging.getLogger(__name__)


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_job(row: JobModel) -> Job:
    return Job(
        job_id=row.job_id,
        status=row.status,
        playbook=row.playbook,
        device=row.device,
        parameters=row.parameters,
        result=row.result,
        error=row.error,
        created_at=_ensure_utc(row.created_at),
        started_at=_ensure_utc(row.started_at),
        finished_at=_ensure_utc(row.finished_at),
        retry_count=row.retry_count,
        max_retries=row.max_retries,
        rollback_performed=row.rollback_performed,
        rollback_success=row.rollback_success,
        pre_state=row.pre_state,
        last_error=row.last_error,
        current_step=row.current_step,
        group_job_id=row.group_job_id,
    )


def create_job(
    playbook: Optional[str] = None,
    device: Optional[str] = None,
    parameters: Optional[dict] = None,
    max_retries: int = 3,
    group_job_id: Optional[str] = None,
) -> Job:
    with get_session() as session:
        row = JobModel(
            job_id=str(uuid.uuid4()),
            status="pending",
            playbook=playbook,
            device=device,
            parameters=parameters,
            max_retries=max_retries,
            created_at=datetime.now(timezone.utc),
            rollback_performed=False,
            retry_count=0,
            group_job_id=group_job_id,
        )
        session.add(row)
        session.flush()
        job = _to_job(row)
    logger.info("Job %s created (playbook=%s device=%s)", job.job_id, playbook, device)
    return job


def get_job(job_id: str) -> Optional[Job]:
    with get_session() as session:
        row = session.query(JobModel).filter_by(job_id=job_id).first()
        return _to_job(row) if row else None


def get_all_jobs() -> list[Job]:
    with get_session() as session:
        rows = session.query(JobModel).order_by(JobModel.created_at.desc()).all()
        return [_to_job(r) for r in rows]


def query_jobs(
    status: Optional[str] = None,
    device: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    site_id: Optional[int] = None,
    allowed_devices: Optional[set[str]] = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Job], int]:
    with get_session() as session:
        q = session.query(JobModel)
        if status is not None:
            q = q.filter(JobModel.status == status)
        if device is not None:
            q = q.filter(JobModel.device == device)
        if from_date is not None:
            fd = from_date if from_date.tzinfo else from_date.replace(tzinfo=timezone.utc)
            q = q.filter(JobModel.created_at >= fd)
        if to_date is not None:
            td = to_date if to_date.tzinfo else to_date.replace(tzinfo=timezone.utc)
            q = q.filter(JobModel.created_at <= td)
        if site_id is not None:
            from app.db.models import DeviceGroupModel, DeviceModel
            device_names = [
                r[0]
                for r in session.query(DeviceModel.name)
                .join(DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id)
                .filter(DeviceGroupModel.site_id == site_id)
                .all()
            ]
            if device_names:
                q = q.filter(JobModel.device.in_(device_names))
            else:
                # No devices in this site → result must be empty.
                return [], 0
        if allowed_devices is not None:
            # Empty set → caller has no visibility; short-circuit to empty result.
            if not allowed_devices:
                return [], 0
            q = q.filter(JobModel.device.in_(allowed_devices))
        total = q.count()
        rows = (
            q.order_by(JobModel.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return [_to_job(r) for r in rows], total


def update_job(
    job_id: str,
    status: Optional[str] = None,
    result: Optional[dict] = None,
    error: Optional[str] = None,
    retry_count: Optional[int] = None,
    rollback_performed: Optional[bool] = None,
    rollback_success: Optional[bool] = None,
    pre_state: Optional[dict] = None,
    last_error: Optional[str] = None,
    current_step: Optional[str] = None,
) -> None:
    with get_session() as session:
        row = session.query(JobModel).filter_by(job_id=job_id).first()
        if not row:
            return
        if status is not None:
            row.status = status
        if result is not None:
            row.result = result
        if error is not None:
            row.error = error
        if retry_count is not None:
            row.retry_count = retry_count
        if rollback_performed is not None:
            row.rollback_performed = rollback_performed
        if rollback_success is not None:
            row.rollback_success = rollback_success
        if pre_state is not None:
            row.pre_state = pre_state
        if last_error is not None:
            row.last_error = last_error

        now = datetime.now(timezone.utc)
        if status == "running":
            row.started_at = now
            row.current_step = "executing"
            logger.info("Job %s started", job_id)
        elif status == "completed":
            row.finished_at = now
            row.current_step = "completed"
            duration = (now - _ensure_utc(row.started_at)).total_seconds() if row.started_at else 0.0
            logger.info(
                "Job %s completed in %.2fs (retries=%d)",
                job_id, duration, row.retry_count,
            )
        elif status == "failed":
            row.finished_at = now
            row.current_step = "failed"
            duration = (now - _ensure_utc(row.started_at)).total_seconds() if row.started_at else 0.0
            logger.warning(
                "Job %s failed after %.2fs (retries=%d rollback=%s): %s",
                job_id, duration, row.retry_count, row.rollback_performed, error or result,
            )

        # Explicit current_step overrides the status-derived value above
        if current_step is not None:
            row.current_step = current_step


def ensure_final_state(job_id: str) -> None:
    """Force any non-terminal job to failed. Called in finally blocks to prevent stuck states."""
    with get_session() as session:
        row = session.query(JobModel).filter_by(job_id=job_id).first()
        if row and row.status not in ("completed", "failed", "cancelled"):
            logger.warning("Job %s stuck in '%s' — forcing to failed", job_id, row.status)
            row.status = "failed"
            if not row.error:
                row.error = "Unexpected termination"
            row.finished_at = datetime.now(timezone.utc)


def cancel_job(job_id: str) -> Optional[Job]:
    with get_session() as session:
        row = session.query(JobModel).filter_by(job_id=job_id).first()
        if not row:
            return None
        if row.status in ("pending", "running"):
            row.status = "cancelled"
            row.finished_at = datetime.now(timezone.utc)
            logger.info("Job %s cancelled", job_id)
        return _to_job(row)


def mark_orphaned_jobs_failed() -> int:
    """Mark any job still in 'running' state as failed. Called once on startup."""
    with get_session() as session:
        orphaned = (
            session.query(JobModel)
            .filter(JobModel.status == "running")
            .all()
        )
        now = datetime.now(timezone.utc)
        for row in orphaned:
            row.status = "failed"
            row.error = "Server restarted while job was running"
            row.finished_at = now
        count = len(orphaned)
    if count:
        logger.warning("Marked %d orphaned job(s) as failed on startup", count)
    return count
