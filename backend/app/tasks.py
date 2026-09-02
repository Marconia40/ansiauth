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
task/camino especial mientras tanto.

``sync_device_task`` (cache-first read model) — corre DeviceSyncService
en un worker separado para que el POST del alta y los endpoints de
refresh no bloqueen esperando al equipo. Ver device_sync_service.py."""
import logging

from app.worker import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="ansiauth.orquestador.ejecutar")
def ejecutar_task(recurso_dict: dict, tipo_recurso: str, device_name: str, actor: str, job_id: str) -> None:
    from app.composition import job_repository, orquestador
    from app.models.port import Puerto
    from app.models.vlan import VLAN

    _TIPOS = {"vlan": VLAN, "puerto": Puerto}
    recurso = _TIPOS[tipo_recurso](**recurso_dict)
    job = job_repository.get(job_id)
    orquestador.ejecutar(recurso, device_name, actor, job)


@celery_app.task(name="ansiauth.device.sync")
def sync_device_task(device_name: str, scope: str) -> None:
    """Sincroniza la caché en DB (``device_vlans`` / ``device_ports``) con
    el estado en vivo del equipo. ``scope`` ∈ ``{"vlans", "ports", "all"}``.

    Disparado por:
    * ``Inventory.register()`` con ``scope="all"`` -- alta de device
      (fire-and-forget, el POST vuelve al toque).
    * ``POST /devices/{name}/{vlans,ports}/refresh`` -- refresh manual
      granular desde la UI.
    * ``Orquestador`` al completar OK un job -- coherencia post-escritura.

    ``"all"`` corre ambos scopes secuencialmente; si uno falla, el otro
    igual corre (son independientes -- que las VLANs no se puedan leer
    no es razón para no leer los puertos). Si alguno falló, la task
    termina en error para que Celery/observabilidad lo vean; los detalles
    ya quedaron persistidos en ``devices.{vlans,ports}_sync_error`` por
    el propio servicio, así que el frontend los muestra sin depender del
    resultado del task.
    """
    from app.composition import device_repository, device_sync_service

    device = device_repository.get(device_name)
    if device is None:
        # Alta seguida de baja rápida, o refresh sobre un device borrado
        # concurrentemente. No es un fallo — no hay nada que sincronizar.
        logger.warning("sync_device_task: device '%s' no existe, saltando", device_name)
        return

    if scope == "vlans":
        device_sync_service.sync_vlans(device)
    elif scope == "ports":
        device_sync_service.sync_ports(device)
    elif scope == "all":
        errores: list[str] = []
        try:
            device_sync_service.sync_vlans(device)
        except Exception as exc:
            errores.append(f"vlans: {exc}")
        try:
            device_sync_service.sync_ports(device)
        except Exception as exc:
            errores.append(f"ports: {exc}")
        if errores:
            raise RuntimeError(
                f"sync_device_task[all] device={device_name} partial failure: "
                + "; ".join(errores)
            )
    else:
        raise ValueError(
            f"sync_device_task: scope inválido {scope!r} (esperado: vlans / ports / all)"
        )
