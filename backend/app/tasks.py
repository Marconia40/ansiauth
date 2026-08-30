"""Tasks de Celery — ``ejecutar_task`` es el genérico para cualquier
RecursoGestionable (FASE_5.md A5, reemplaza los 11 tasks reales de
vlan_execution_service.py/port_execution_service.py/port_config_service.py,
ninguno referenciado por nombre-string desde otro lado, confirmado con
grep — sin ruptura externa). ``guardar_config_task`` (Fase 7) es la única
excepción -- "guardar configuración" no es un RecursoGestionable, ver su
docstring."""
from app.worker import celery_app


@celery_app.task(name="ansiauth.orquestador.ejecutar")
def ejecutar_task(recurso_dict: dict, tipo_recurso: str, device_name: str, actor: str, job_id: str) -> None:
    from app.composition import job_repository, orquestador
    from app.models.port import Puerto
    from app.models.vlan import VLAN

    _TIPOS = {"vlan": VLAN, "puerto": Puerto}
    recurso = _TIPOS[tipo_recurso](**recurso_dict)
    job = job_repository.get(job_id)
    orquestador.ejecutar(recurso, device_name, actor, job)


@celery_app.task(name="ansiauth.device.guardar_config")
def guardar_config_task(device_name: str, actor: str, job_id: str) -> None:
    """Reemplaza vlan_execution_service.py: _save_task()/run_save_job() --
    Fase 7, encontrado que POST /devices/{name}/save (`enqueue_save_job()`)
    despachaba a un task Celery (`ansiauth.vlan.save`) que ningún worker real
    tiene registrado desde que Fase 5/A5 cambió `worker.py: include=` a
    `["app.tasks"]` únicamente -- el endpoint respondía 200 con un job_id,
    pero el guardado real nunca corría. "Guardar configuración" no es un
    RecursoGestionable (no hay Repository al que persistir el resultado), así
    que no pasa por `ejecutar_task`/`GroupOperationRunner` -- usa
    `Orquestador.ejecutar_comando()`, la variante sin recurso (mismo lock +
    retry/clasificación de errores + Job, sin validar()/reconciliar()/
    aplicar() ni el paso final de repos[...].add())."""
    from app.composition import job_repository, orquestador

    job = job_repository.get(job_id)
    if job is None:
        return
    orquestador.ejecutar_comando(
        lambda device: device.driver.save_config(device, device.password),
        device_name, actor, job, "device_config_guardada",
    )
