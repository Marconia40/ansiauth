"""Tasks de Celery — ``ejecutar_task`` es el único genérico, para cualquier
RecursoGestionable (FASE_5.md A5, reemplaza los 11 tasks reales de
vlan_execution_service.py/port_execution_service.py/port_config_service.py,
ninguno referenciado por nombre-string desde otro lado, confirmado con
grep — sin ruptura externa).

``guardar_config_task`` (Fase 7, "guardar configuración" sin
RecursoGestionable detrás) se retiró en esta sesión junto con
``Orquestador.ejecutar_comando()`` y ``POST /devices/{name}/save``
(``api/devices.py``) -- la feature queda inactiva hasta que se
implemente como RecursoGestionable de verdad, en vez de mantener un 2do
task/camino especial mientras tanto."""
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
