import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_

from app.db.models import AuditLogModel, DeviceModel
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


def _apply_filters(
    q,
    *,
    session,
    user: Optional[str],
    action: Optional[str],
    resource: Optional[str],
    status: Optional[str],
    from_date: Optional[datetime],
    to_date: Optional[datetime],
    device_id: Optional[str],
    site_id: Optional[int],
    allowed_devices: Optional[set[str]] = None,
):
    if user:
        q = q.filter(AuditLogModel.user == user)
    if action:
        q = q.filter(AuditLogModel.action == action)
    if resource:
        q = q.filter(AuditLogModel.resource == resource)
    if status:
        q = q.filter(AuditLogModel.status == status)
    if from_date is not None:
        _from = from_date if from_date.tzinfo else from_date.replace(tzinfo=timezone.utc)
        q = q.filter(AuditLogModel.timestamp >= _from)
    if to_date is not None:
        _to = to_date if to_date.tzinfo else to_date.replace(tzinfo=timezone.utc)
        q = q.filter(AuditLogModel.timestamp <= _to)
    if device_id is not None:
        q = q.filter(AuditLogModel.device == device_id)
    if site_id is not None:
        # Show device-related rows whose device is owned by `site_id`, plus rows
        # that are not tied to a device at all (per spec: "If action is unrelated
        # to device: keep visible").
        device_names = [
            r[0] for r in session.query(DeviceModel.name).filter(DeviceModel.site_id == site_id).all()
        ]
        if device_names:
            q = q.filter(or_(AuditLogModel.device.is_(None), AuditLogModel.device.in_(device_names)))
        else:
            # Empty site → only keep rows unrelated to a device.
            q = q.filter(AuditLogModel.device.is_(None))
    if allowed_devices is not None:
        # Restricted (non-admin) caller: keep rows for their devices + device-less
        # rows (login, user-management, etc.). Spec: device-unrelated entries stay visible.
        if not allowed_devices:
            q = q.filter(AuditLogModel.device.is_(None))
        else:
            q = q.filter(or_(AuditLogModel.device.is_(None), AuditLogModel.device.in_(allowed_devices)))
    return q


def get_audit_log(
    user: Optional[str] = None,
    action: Optional[str] = None,
    resource: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    device_id: Optional[str] = None,
    site_id: Optional[int] = None,
    allowed_devices: Optional[set[str]] = None,
    skip: int = 0,
    limit: int = 100,
) -> list[AuditRecord]:
    with get_session() as session:
        q = _apply_filters(
            session.query(AuditLogModel).order_by(AuditLogModel.timestamp.desc()),
            session=session,
            user=user,
            action=action,
            resource=resource,
            status=status,
            from_date=from_date,
            to_date=to_date,
            device_id=device_id,
            site_id=site_id,
            allowed_devices=allowed_devices,
        )
        rows = q.offset(skip).limit(limit).all()
        return [_to_record(r) for r in rows]


def count_audit_log(
    user: Optional[str] = None,
    action: Optional[str] = None,
    resource: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    device_id: Optional[str] = None,
    site_id: Optional[int] = None,
    allowed_devices: Optional[set[str]] = None,
) -> int:
    with get_session() as session:
        q = _apply_filters(
            session.query(AuditLogModel),
            session=session,
            user=user,
            action=action,
            resource=resource,
            status=status,
            from_date=from_date,
            to_date=to_date,
            device_id=device_id,
            site_id=site_id,
            allowed_devices=allowed_devices,
        )
        return q.count()


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


def purge_old_records(retention_days: int, triggered_by: str = "scheduler") -> int:
    """Delete audit records older than retention_days. Returns the number of rows deleted.

    Deletes in chunks of 1000 to avoid long table locks on SQLite.
    Records that are parents of newer rows are preserved to keep chain integrity.
    Logs a system audit event after purge.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    total_deleted = 0

    while True:
        with get_session() as session:
            # Find IDs that are referenced as parent_audit_id — never delete these
            referenced_ids = {
                row[0]
                for row in session.query(AuditLogModel.parent_audit_id)
                .filter(AuditLogModel.parent_audit_id.isnot(None))
                .all()
            }
            batch_ids = [
                row[0]
                for row in session.query(AuditLogModel.id)
                .filter(
                    AuditLogModel.timestamp < cutoff,
                    AuditLogModel.id.notin_(referenced_ids) if referenced_ids else True,
                )
                .limit(1000)
                .all()
            ]
            if not batch_ids:
                break
            deleted = (
                session.query(AuditLogModel)
                .filter(AuditLogModel.id.in_(batch_ids))
                .delete(synchronize_session=False)
            )
            total_deleted += deleted

    if total_deleted > 0 or triggered_by != "scheduler":
        log_action(
            user="system",
            action="audit_purge",
            resource="audit_log",
            details={
                "retention_days": retention_days,
                "deleted_count": total_deleted,
                "triggered_by": triggered_by,
            },
            status="success",
        )
        logger.info(
            "Audit purge complete: deleted=%d retention_days=%d triggered_by=%s",
            total_deleted, retention_days, triggered_by,
        )
    return total_deleted


def clear_audit_log() -> None:
    """Delete all audit records. Used in tests."""
    with get_session() as session:
        session.query(AuditLogModel).delete()
    logger.debug("Audit log cleared")
