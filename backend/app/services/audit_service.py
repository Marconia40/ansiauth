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
        rows = q.offset(skip).limit(limit).all()
        return [_to_record(r) for r in rows]


def update_audit_status(audit_id: str, status: str) -> None:
    """Update the status of an existing audit record after job execution."""
    with get_session() as session:
        row = session.query(AuditLogModel).filter_by(id=int(audit_id)).first()
        if row:
            row.status = status
    logger.debug("Audit %s status → %s", audit_id, status)


def update_audit_record(
    audit_id: str,
    status: str,
    extra_details: Optional[dict] = None,
) -> None:
    """Update status and merge execution metadata into the audit record details."""
    with get_session() as session:
        row = session.query(AuditLogModel).filter_by(id=int(audit_id)).first()
        if row:
            row.status = status
            if extra_details:
                row.details = {**(row.details or {}), **extra_details}
    logger.debug("Audit %s: status=%s extra=%s", audit_id, status, extra_details)


def ensure_audit_final_state(audit_id: str) -> None:
    """Force any pending/stuck audit record to failed. Called in finally blocks."""
    with get_session() as session:
        row = session.query(AuditLogModel).filter_by(id=int(audit_id)).first()
        if row and row.status not in ("completed", "failed", "cancelled"):
            logger.warning("Audit %s stuck in '%s' — forcing to failed", audit_id, row.status)
            row.status = "failed"
            row.details = {**(row.details or {}), "error": {"type": "unexpected_termination"}}


def clear_audit_log() -> None:
    """Delete all audit records. Used in tests."""
    with get_session() as session:
        session.query(AuditLogModel).delete()
    logger.debug("Audit log cleared")
