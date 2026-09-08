from dataclasses import asdict


class JobQueue:
    """Wrapper delgado sobre Celery — FINAL_ARCHITECTURE.md ya lo lista como
    interfaz. Único punto donde ``recurso`` cruza a la cola serializado
    (Celery serializa a JSON, ``task_serializer="json"`` en worker.py).

    ``dispatch()`` ya no toma un ``nombre_task`` -- bug real encontrado en
    una revisión de código: existía un parámetro con ese nombre pero el
    cuerpo nunca lo usaba (importaba ``ejecutar_task`` directo y llamaba
    ``.delay()``), y el string que pasaba el único caller
    (``"orquestador.ejecutar"``) ni siquiera coincidía con el nombre real
    registrado en Celery (``"ansiauth.orquestador.ejecutar"``,
    ``app/tasks.py``). Se saca en vez de arreglar el string: el ruteo por
    nombre de Celery ya lo resuelve el decorator ``@celery_app.task(name=
    ...)``, no hace falta que este wrapper también sepa el nombre."""

    def dispatch(self, recurso: "RecursoGestionable", device_name: str, actor: str, job_id: str) -> None:
        from app.tasks import ejecutar_task
        ejecutar_task.delay(asdict(recurso), recurso.repositorio(), device_name, actor, job_id)

    def dispatch_lote(self, recursos: "list[RecursoGestionable]", device_name: str, actor: str, job_id: str) -> None:
        """Ver ``GroupOperationRunner.encolar_lote()``. Mismo criterio de
        serialización (Celery -> JSON) que ``dispatch()``, solo que
        manda una LISTA de recursos en vez de 1."""
        from app.tasks import ejecutar_lote_task
        recursos_dict = [asdict(r) for r in recursos]
        ejecutar_lote_task.delay(recursos_dict, recursos[0].repositorio(), device_name, actor, job_id)
