"""AuditRepository — append-only + scoped read for the audit log.

append() is insert-strict (session.add(), not session.merge()) because
audit rows are historical evidence — a duplicate id has to fail loud,
not silently overwrite. app/db/audit_guard.py enforces the same rule at
the SQLAlchemy level as a defense-in-depth.

query()/count() take a pre-resolved VisibilityScope instead of the
per-call viewer dict the old audit_service.get_audit_log took, and the
scope filter delegates to device_repository.nombres_visibles /
device_group_repository.grupos_visibles — the two joins that
VisibilityScope alone cannot answer.

audit_service.py stayed live and untouched when this repository was
added (Fase 3) — additive at the time. Fase 7 rewired every remaining
real caller (role_assignment_service.py, api/users.py, api/auth.py,
api/audit.py, api/devices.py's save-config path) and deleted
audit_service.py — this repository is now the sole audit write/read
surface.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, or_

from app.core.repository import Repository
from app.db.models import (
    AuditLogModel,
    DeviceGroupModel,
    DeviceModel,
)
from app.db.session import get_session
from app.models.audit import AuditRecord
from app.models.visibility_scope import VisibilityScope


def _to_domain(row: AuditLogModel) -> AuditRecord:
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


class AuditRepository(Repository):
    def __init__(self):
        # to_orm=None on purpose: append() does not go through
        # Repository.add() (which would call session.merge()). Any
        # accidental call to the inherited add() fails loud with
        # "NoneType is not callable" instead of silently upserting an
        # audit record.
        super().__init__(AuditLogModel, _to_domain, to_orm=None)

    def append(self, record: AuditRecord) -> AuditRecord:
        """Insert strict — never merge. AuditRecord.id from the caller is
        discarded (the DB assigns it); the returned record carries the
        real DB id."""
        with get_session() as session:
            row = AuditLogModel(
                timestamp=record.timestamp,
                user=record.user,
                action=record.action,
                resource=record.resource,
                resource_id=record.resource_id,
                details=record.details,
                status=record.status,
                job_id=record.job_id,
                device=record.device,
                request_id=record.request_id,
            )
            session.add(row)
            session.flush()
            return _to_domain(row)

    def query(
        self,
        *,
        user: Optional[str] = None,
        action: Optional[str] = None,
        resource: Optional[str] = None,
        status: Optional[str] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        device_id: Optional[str] = None,
        site_id: Optional[int] = None,
        scope: VisibilityScope,
        page: int = 1,
        page_size: int = 100,
        offset: Optional[int] = None,
    ) -> tuple[list[AuditRecord], int]:
        """``offset``, cuando viene, gana sobre ``page`` para el cálculo de
        SQL OFFSET -- necesario para el modo ``skip``/``limit`` (raw
        offsets) de ``GET /audit``: convertir un ``skip`` arbitrario a
        ``page`` vía ``(skip // page_size) + 1`` solo da el offset exacto
        cuando ``skip`` es múltiplo de ``page_size`` -- cualquier otro
        valor perdía el resto en silencio (bug real encontrado en una
        revisión de código)."""
        with get_session() as session:
            q = session.query(AuditLogModel).order_by(AuditLogModel.timestamp.desc())
            q = self._aplicar_filtros(
                q, session,
                user=user, action=action, resource=resource, status=status,
                from_date=from_date, to_date=to_date,
                device_id=device_id, site_id=site_id,
            )
            q = self._aplicar_scope(q, session, scope)
            total = q.count()
            real_offset = offset if offset is not None else (page - 1) * page_size
            rows = q.offset(real_offset).limit(page_size).all()
            return [_to_domain(r) for r in rows], total

    def count(
        self,
        *,
        user: Optional[str] = None,
        action: Optional[str] = None,
        resource: Optional[str] = None,
        status: Optional[str] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        device_id: Optional[str] = None,
        site_id: Optional[int] = None,
        scope: VisibilityScope,
    ) -> int:
        with get_session() as session:
            q = self._aplicar_filtros(
                session.query(AuditLogModel), session,
                user=user, action=action, resource=resource, status=status,
                from_date=from_date, to_date=to_date,
                device_id=device_id, site_id=site_id,
            )
            q = self._aplicar_scope(q, session, scope)
            return q.count()

    def purge_old(self, retention_days: int, triggered_by: str = "scheduler") -> int:
        """Delete audit rows older than *retention_days*. Rows referenced
        as parent_audit_id are preserved to keep chain integrity.
        Deletes in chunks of 1000 to avoid long table locks on SQLite.
        Appends a system audit record after a non-empty purge."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        total_deleted = 0

        while True:
            with get_session() as session:
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
            self.append(AuditRecord(
                user="system",
                action="audit_purge",
                resource="audit_log",
                details={
                    "retention_days": retention_days,
                    "deleted_count": total_deleted,
                    "triggered_by": triggered_by,
                },
            ))
        return total_deleted

    # ─── Internal helpers ──────────────────────────────────────────────

    def _aplicar_filtros(
        self, q, session, *,
        user, action, resource, status,
        from_date, to_date, device_id, site_id,
    ):
        """Simple filters — lifted from audit_service._apply_filters,
        minus the viewer/allowed_devices branch (scope filtering runs in
        _aplicar_scope instead)."""
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
            # Rows about devices in this site, plus rows unrelated to
            # any device (per spec: "if action is unrelated to device,
            # keep visible").
            nombres_site = [
                r[0]
                for r in session.query(DeviceModel.name)
                .join(DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id)
                .filter(DeviceGroupModel.site_id == site_id)
                .all()
            ]
            if nombres_site:
                q = q.filter(or_(AuditLogModel.device.is_(None), AuditLogModel.device.in_(nombres_site)))
            else:
                q = q.filter(AuditLogModel.device.is_(None))
        return q

    def _aplicar_scope(self, q, session, scope: VisibilityScope):
        """Scope filter — delegates the two joins that VisibilityScope
        alone cannot answer to device_repository / device_group_repository."""
        if scope.es_system_admin:
            return q
        # Lazy import: composition imports this class at module load —
        # top-level import from app.composition would be circular.
        from app.composition import device_group_repository, device_repository

        nombres = device_repository.nombres_visibles(scope)
        grupos = device_group_repository.grupos_visibles(scope)
        conds = []
        if nombres:
            conds.append(AuditLogModel.device.in_(nombres))
        conds.append(and_(AuditLogModel.device.is_(None), AuditLogModel.resource == "auth"))
        if grupos:
            conds.append(and_(
                AuditLogModel.resource == "device_group",
                AuditLogModel.resource_id.in_({str(g) for g in grupos}),
            ))
        conds.append(and_(
            AuditLogModel.resource == "site",
            AuditLogModel.resource_id.in_([str(s) for s in (scope.site_ids or set())]),
        ))
        # role_assignment (grant/revoke/update) had no branch at all here --
        # bug real encontrado en una revisión de código: cualquier
        # site-admin/group-admin que otorga/revoca un rol dentro de su
        # propio scope (D25 se lo permite) nunca veía esa fila en su propio
        # /audit, ni la suya ni la de otro admin del mismo site. resource_id
        # acá es el id del propio grant, no un site/group id -- no puede
        # reusar el patrón de arriba, así que compara el `site_id` que ya
        # viaja en el payload JSON (`details`) contra los sites visibles del
        # caller.
        if scope.site_ids:
            conds.append(and_(
                AuditLogModel.resource == "role_assignment",
                AuditLogModel.details["site_id"].as_integer().in_(list(scope.site_ids)),
            ))
        return q.filter(or_(*conds))
