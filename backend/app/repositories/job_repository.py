from datetime import datetime, timezone
from typing import Optional

from app.core.repository import Repository
from app.db.models import JobModel
from app.db.session import get_session
from app.models.job import Job
from app.models.visibility_scope import VisibilityScope


def _ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_domain(row: JobModel) -> Job:
    return Job(
        job_id=row.job_id,
        status=row.status,
        operation=row.operation,
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


def _to_orm(j: Job) -> JobModel:
    return JobModel(
        job_id=j.job_id, status=j.status, operation=j.operation, playbook=j.playbook,
        device=j.device, parameters=j.parameters, result=j.result, error=j.error,
        created_at=j.created_at, started_at=j.started_at, finished_at=j.finished_at,
        retry_count=j.retry_count, max_retries=j.max_retries,
        rollback_performed=j.rollback_performed, rollback_success=j.rollback_success,
        pre_state=j.pre_state, last_error=j.last_error, current_step=j.current_step,
        group_job_id=j.group_job_id,
    )


class JobRepository(Repository):
    def __init__(self):
        super().__init__(JobModel, _to_domain, _to_orm, pk_field="job_id")

    def activo_para(self, device: str) -> bool:
        """¿Hay un Job pending/running para este device? Usado por
        Inventory.move() (FINAL_ARCHITECTURE.md §1, nota 1/2) para bloquear
        mover un device con trabajo en curso."""
        with get_session() as session:
            existe = (
                session.query(JobModel)
                .filter(JobModel.device == device, JobModel.status.in_(("pending", "running")))
                .first()
            )
            return existe is not None

    def recuperar_huerfanos(self) -> int:
        """Reemplaza job_service.py: mark_orphaned_jobs_failed() -- copia la
        lógica tal cual, usando Job.asegurar_estado_final() en vez de mutar
        status/error/finished_at a mano."""
        with get_session() as session:
            filas = session.query(JobModel).filter(JobModel.status == "running").all()
            n = 0
            for row in filas:
                job = _to_domain(row)
                job.asegurar_estado_final()
                row.status = job.status
                row.error = job.error
                row.finished_at = job.finished_at
                n += 1
            return n

    def resumen_de_grupo(self, group_job_id: str) -> "dict | None":
        """Reemplaza GroupJob completo (A2) -- agrega los Job reales al vuelo,
        sin estado duplicado. Devuelve None si no hay ningún Job con este
        group_job_id (equivalente al 404 que hoy da get_group_job() real).

        Forma exacta verificada contra frontend/src/types/job.ts (GroupJob/
        GroupJobExecutionSummary/GroupJobDeviceResult)."""
        jobs = self.list(group_job_id=group_job_id)
        if not jobs:
            return None
        completed = sum(1 for j in jobs if j.status == "completed")
        failed = sum(1 for j in jobs if j.status == "failed")
        rollback_count = sum(1 for j in jobs if j.rollback_performed)
        estados = {j.status for j in jobs}
        todos_terminales = all(j.esta_en_estado_terminal() for j in jobs)
        if estados <= {"pending"}:
            status_grupo = "pending"   # ningún device arrancó todavía
        elif not todos_terminales:
            status_grupo = "running"
        elif failed == 0:
            status_grupo = "completed"
        elif completed == 0:
            status_grupo = "failed"
        else:
            status_grupo = "partial_success"
        primero = jobs[0]
        started = [j.started_at for j in jobs if j.started_at]
        finished = [j.finished_at for j in jobs if j.finished_at]
        duration_ms_grupo = (
            round((max(finished) - min(started)).total_seconds() * 1000)
            if started and finished and todos_terminales else None
        )

        def _duration_ms(j: "Job") -> "int | None":
            if j.started_at and j.finished_at:
                return round((j.finished_at - j.started_at).total_seconds() * 1000)
            return None

        return {
            "group_job_id": group_job_id,
            "status": status_grupo,
            "operation": primero.operation,
            "playbook": primero.playbook,
            "parameters": primero.parameters,
            "created_at": min((j.created_at for j in jobs), default=None),
            "started_at": min(started, default=None),
            "finished_at": max(finished, default=None) if todos_terminales else None,
            "execution_summary": {
                "total_devices": len(jobs),
                "completed": completed,
                "failed": failed,
                "partial_success": completed > 0 and failed > 0,
                "rollback_count": rollback_count,
                "duration_ms": duration_ms_grupo,
            },
            "device_results": [
                {
                    "device": j.device, "job_id": j.job_id, "status": j.status,
                    "current_step": j.current_step, "retry_count": j.retry_count,
                    "rollback_performed": j.rollback_performed,
                    "rollback_success": j.rollback_success, "error": j.error,
                    "duration_ms": _duration_ms(j),
                }
                for j in jobs
            ],
        }

    def query(
        self,
        *,
        status: Optional[str] = None,
        device: Optional[str] = None,
        site_id: Optional[int] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        scope: VisibilityScope,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[Job], int]:
        """Paginated read of jobs, with authorization applied.

        ``device`` and ``site_id`` are explicit user filters ("of what I
        can see, show me only site X") — NOT authorization. Authorization
        runs against ``scope`` via DeviceRepository.nombres_visibles():
        rows whose device is outside the caller's visible set are
        dropped. system-admin skips the visibility filter entirely.

        Frontend contract: frontend/src/services/api.ts:getJobs sends
        ``status``/``device``/``site_id`` as query params separate from
        the JWT — this shape mirrors that.
        """
        # Lazy imports: composition.py imports this module at load time
        # (job_repository singleton), so a top-level `from app.composition
        # import device_repository` would be circular. Same trick as
        # AuditRepository._aplicar_scope.
        from app.composition import device_repository

        with get_session() as session:
            q = session.query(JobModel)
            if status is not None:
                q = q.filter(JobModel.status == status)
            if device is not None:
                q = q.filter(JobModel.device == device)
            if from_date is not None:
                q = q.filter(JobModel.created_at >= from_date)
            if to_date is not None:
                q = q.filter(JobModel.created_at <= to_date)
            if site_id is not None:
                from app.db.models import DeviceGroupModel, DeviceModel
                nombres_site = [
                    r[0]
                    for r in session.query(DeviceModel.name)
                    .join(DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id)
                    .filter(DeviceGroupModel.site_id == site_id)
                    .all()
                ]
                if not nombres_site:
                    return [], 0
                q = q.filter(JobModel.device.in_(nombres_site))
            if not scope.es_system_admin:
                nombres = device_repository.nombres_visibles(scope)
                if nombres is not None:
                    if not nombres:
                        return [], 0
                    q = q.filter(JobModel.device.in_(nombres))
            total = q.count()
            rows = (
                q.order_by(JobModel.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            return [_to_domain(r) for r in rows], total
