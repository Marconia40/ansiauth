import logging
from datetime import datetime, timezone
from typing import Optional

from app.db.models import AuditLogModel
from app.db.session import get_session
from app.models.audit import AuditRecord

logger = logging.getLogger(__name__)


def _to_record(row: AuditLogModel) -> AuditRecord:
    return AuditRecord(
        id=str(row.id),
        timestamp=row.timestamp,
        user=row.user,
        action=row.action,
        resource=row.resource,
        resource_id=row.resource_id,
        details=row.details if row.details else {},
        status=row.status,
        job_id=row.job_id,
        device=row.device,
        request_id=row.request_id,
        parent_audit_id=str(row.parent_audit_id) if row.parent_audit_id is not None else None,
    )


def log_action(
    user: str,
    action: str,
    resource: str,
    details: dict,
    status: str = "success",
    job_id: Optional[str] = None,
    resource_id: Optional[str] = None,
    device: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AuditRecord:
    with get_session() as session:
        row = AuditLogModel(
            timestamp=datetime.now(timezone.utc),
            user=user,
            action=action,
            resource=resource,
            resource_id=resource_id,
            details=details,
            status=status,
            job_id=job_id,
            device=device,
            request_id=request_id,
        )
        session.add(row)
        session.flush()
        record = _to_record(row)
    logger.info("Audit: user=%s action=%s resource=%s status=%s", user, action, resource, status)
    return record


def get_audit_log(
    user: Optional[str] = None,
    action: Optional[str] = None,
    resource: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    device_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
) -> list[AuditRecord]:
    with get_session() as session:
        q = session.query(AuditLogModel).order_by(AuditLogModel.timestamp.desc())
        if user:
            q = q.filter(AuditLogModel.user == user)
        if action:
            q = q.filter(AuditLogModel.action == action)
        if resource:
            q = q.filter(AuditLogModel.resource == resource)
        if from_date is not None:
            _from = from_date if from_date.tzinfo else from_date.replace(tzinfo=timezone.utc)
            q = q.filter(AuditLogModel.timestamp >= _from)
        if to_date is not None:
            _to = to_date if to_date.tzinfo else to_date.replace(tzinfo=timezone.utc)
            q = q.filter(AuditLogModel.timestamp <= _to)
        if device_id is not None:
            q = q.filter(AuditLogModel.device == device_id)
        rows = q.offset(skip).limit(limit).all()
        return [_to_record(r) for r in rows]


def append_audit_event(
    parent_audit_id: str,
    status: str,
    extra_details: Optional[dict] = None,
) -> Optional[AuditRecord]:
    """Append a new status-event row linked to an existing audit record. Never mutates."""
    with get_session() as session:
        parent = session.query(AuditLogModel).filter_by(id=int(parent_audit_id)).first()
        if not parent:
            logger.warning("append_audit_event: parent %s not found", parent_audit_id)
            return None
        row = AuditLogModel(
            timestamp=datetime.now(timezone.utc),
            user=parent.user,
            action=parent.action,
            resource=parent.resource,
            resource_id=parent.resource_id,
            details={**(parent.details or {}), **(extra_details or {})},
            status=status,
            job_id=parent.job_id,
            device=parent.device,
            request_id=parent.request_id,
            parent_audit_id=int(parent_audit_id),
        )
        session.add(row)
        session.flush()
        record = _to_record(row)
    logger.debug("Audit %s: appended event status=%s parent=%s", record.id, status, parent_audit_id)
    return record


def ensure_audit_final_state(audit_id: str) -> None:
    """Append a 'failed' event if no terminal follow-up exists yet. Safety net for unexpected exits."""
    with get_session() as session:
        already_terminal = (
            session.query(AuditLogModel)
            .filter(
                AuditLogModel.parent_audit_id == int(audit_id),
                AuditLogModel.status.in_(["completed", "failed", "cancelled"]),
            )
            .first()
        )
        if already_terminal:
            return
    logger.warning("Audit %s: no terminal event found — appending 'failed'", audit_id)
    append_audit_event(audit_id, "failed", {"error": {"type": "unexpected_termination"}})


def clear_audit_log() -> None:
    """Delete all audit records. Used in tests."""
    with get_session() as session:
        session.query(AuditLogModel).delete()
    logger.debug("Audit log cleared")
