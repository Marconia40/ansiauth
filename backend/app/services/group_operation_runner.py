import uuid
from dataclasses import asdict

from app.models.job import Job


class GroupOperationRunner:
    """FINAL_ARCHITECTURE.md §2.9 (Saga) — fan-out real de
    vlan_execution_service.py/port_execution_service.py/port_config_service.py.
    Dispatch paralelo real (decisión de FASE_5.md): un Celery task por
    device, no uno por grupo. No sostiene ningún lock — encolar() es
    síncrono y no toca el device, el RedisCoordinator vive en Orquestador
    (ver FASE_5.md A3/A4, FINAL_ARCHITECTURE.md §2.4 nota (12))."""

    def __init__(self, orquestador, job_queue, jobs):
        self._orquestador = orquestador
        self._job_queue = job_queue      # JobQueue, A5 -- wrapper sobre Celery
        self._jobs = jobs                # JobRepository

    def encolar(self, recurso: "RecursoGestionable", devices: list[str], actor: str) -> tuple[str, list[dict]]:
        group_job_id = str(uuid.uuid4())  # sin fila, sin Repository[GroupJob] -- §2.9
        parametros = asdict(recurso)  # Job.parameters -- JobDetailModal.tsx real lo necesita
        resumen = recurso.resumen_intento()  # Job.parameters_summary -- ver docstring del campo
        job_entries = []
        for device_name in devices:
            job = Job(
                operation=recurso.repositorio(), device=device_name,
                group_job_id=group_job_id, parameters=parametros,
                parameters_summary=resumen,
            )
            self._jobs.add(job)
            self._job_queue.dispatch(recurso, device_name, actor, job.job_id)
            job_entries.append({"device": device_name, "job_id": job.job_id, "status": job.status})
        return group_job_id, job_entries

    def encolar_lote(self, recursos: "list[RecursoGestionable]", device_name: str, actor: str) -> tuple[str, dict]:
        """Dimensión opuesta a ``encolar()``: 1 *device*, N *recursos* (en
        vez de 1 recurso, N devices) -- ver ``Orquestador.ejecutar_lote()``.
        1 solo ``Job`` para todo el lote (no N) -- es 1 conexión real, se
        trackea como 1 unidad de trabajo. ``group_job_id`` se mantiene por
        consistencia de forma con ``encolar()`` (mismo contrato de
        respuesta que el resto de la API), aunque acá agrupe una lista de
        1 solo job."""
        group_job_id = str(uuid.uuid4())
        parametros = {"lote": [asdict(r) for r in recursos]}
        # Cada resumen ya incluye su propia identidad (vlan_id/interface +
        # device) -- concatenar es suficiente, no hace falta extraer un
        # prefijo común (ver nota en el plan de esta sesión sobre por qué
        # no vale la pena la complejidad extra acá).
        resumen = "; ".join(r.resumen_intento() for r in recursos)
        job = Job(
            operation=recursos[0].repositorio(), device=device_name,
            group_job_id=group_job_id, parameters=parametros,
            parameters_summary=resumen,
        )
        self._jobs.add(job)
        self._job_queue.dispatch_lote(recursos, device_name, actor, job.job_id)
        job_entry = {"device": device_name, "job_id": job.job_id, "status": job.status}
        return group_job_id, job_entry
