from dataclasses import asdict


class JobQueue:
    """Wrapper delgado sobre Celery — FINAL_ARCHITECTURE.md ya lo lista como
    interfaz. Único punto donde ``recurso`` cruza a la cola serializado
    (Celery serializa a JSON, ``task_serializer="json"`` en worker.py)."""

    def dispatch(self, nombre_task: str, recurso: "RecursoGestionable", device_name: str, actor: str, job_id: str) -> None:
        from app.tasks import ejecutar_task
        ejecutar_task.delay(asdict(recurso), recurso.repositorio(), device_name, actor, job_id)
