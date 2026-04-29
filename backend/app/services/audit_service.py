from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from app.models.audit import AuditRecord

_audit_log: list[AuditRecord] = []


def log_action(
    user: str,
    action: str,
    resource: str,
    details: dict,
    status: str = "success",
    job_id: Optional[str] = None,
) -> AuditRecord:
    record = AuditRecord(
        id=str(uuid4()),
        timestamp=datetime.now(timezone.utc),
        user=user,
        action=action,
        resource=resource,
        details=details,
        status=status,
        job_id=job_id,
    )
    _audit_log.append(record)
    return record


def get_audit_log(
    user: Optional[str] = None,
    action: Optional[str] = None,
) -> list[AuditRecord]:
    result = list(_audit_log)
    if user:
        result = [r for r in result if r.user == user]
    if action:
        result = [r for r in result if r.action == action]
    return result


def clear_audit_log() -> None:
    _audit_log.clear()
