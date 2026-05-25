import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.db.models import GroupJobModel
from app.db.session import get_session
from app.models.group_job import DeviceExecution, GroupJob

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_group_job(row: GroupJobModel) -> GroupJob:
    results_raw = row.device_results or []
    return GroupJob(
        group_job_id=row.group_job_id,
        status=row.status,
        operation=row.operation,
        playbook=row.playbook,
        parameters=row.parameters,
        device_results=[DeviceExecution.from_dict(r) for r in results_raw],
        created_at=_ensure_utc(row.created_at),
        started_at=_ensure_utc(row.started_at),
        finished_at=_ensure_utc(row.finished_at),
    )


def create_group_job(
    operation: str,
    playbook: str,
    parameters: dict,
    devices: list[str],
) -> GroupJob:
    group_job_id = str(uuid.uuid4())
    initial_results = [DeviceExecution(device=dev).to_dict() for dev in devices]
    with get_session() as session:
        row = GroupJobModel(
            group_job_id=group_job_id,
            status="pending",
            operation=operation,
            playbook=playbook,
            parameters=parameters,
            device_results=initial_results,
            created_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        group_job = _to_group_job(row)
    logger.info(
        "GroupJob %s created (operation=%s devices=%d)",
        group_job_id, operation, len(devices),
    )
    return group_job


def get_group_job(group_job_id: str) -> Optional[GroupJob]:
    with get_session() as session:
        row = session.query(GroupJobModel).filter_by(group_job_id=group_job_id).first()
        return _to_group_job(row) if row else None


def update_device_result(
    group_job_id: str,
    device: str,
    job_id: str,
    status: str,
    current_step: Optional[str] = None,
    retry_count: int = 0,
    rollback_performed: bool = False,
    rollback_success: Optional[bool] = None,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Update the execution record for one device and re-aggregate group status."""
    with get_session() as session:
        row = session.query(GroupJobModel).filter_by(group_job_id=group_job_id).first()
        if not row:
            logger.warning("GroupJob %s not found — cannot update device=%s", group_job_id, device)
            return

        # Build fresh list of dicts so SQLAlchemy detects the column as dirty
        results = [dict(r) for r in (row.device_results or [])]
        for r in results:
            if r["device"] == device:
                r["job_id"] = job_id
                r["status"] = status
                r["current_step"] = current_step
                r["retry_count"] = retry_count
                r["rollback_performed"] = rollback_performed
                r["rollback_success"] = rollback_success
                r["error"] = error
                r["duration_ms"] = duration_ms
                break

        row.device_results = results

        if row.started_at is None:
            row.started_at = datetime.now(timezone.utc)

        # Aggregate group status from all device results
        statuses = [r["status"] for r in results]
        all_terminal = all(s in _TERMINAL_STATUSES for s in statuses)

        if all_terminal:
            if all(s == "completed" for s in statuses):
                row.status = "completed"
            elif any(s == "completed" for s in statuses):
                row.status = "partial_success"
            else:
                row.status = "failed"
            row.finished_at = datetime.now(timezone.utc)
        else:
            row.status = "running"

    logger.info(
        "GroupJob %s device=%s → status=%s (retry_count=%d rollback=%s)",
        group_job_id, device, status, retry_count, rollback_performed,
    )
