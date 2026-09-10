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
refresh no bloqueen esperando al equipo. Ver device_sync_service.py.

``sync_stale_devices_task`` (Celery Beat, cada
``SYNC_STALE_INTERVAL_MIN`` min) — recorre todos los devices ordenados
por antigüedad de sync y encola ``sync_device_task(name, "all")`` sobre
los que superan el umbral ``SYNC_STALE_THRESHOLD_S``, con
staggering ``SYNC_STALE_STAGGER_S`` para no llenar la cola de golpe.
El semáforo global de SSH (``RedisCoordinator.adquirir_slot``) es la
red de seguridad final -- este staggering es un buen ciudadano
adicional del Celery broker."""
import logging
import os

from app.worker import celery_app

logger = logging.getLogger(__name__)

# ── Scheduler config (Beat) ───────────────────────────────────────────────
# Cadencia base del scheduler que refresca devices "viejos". Ajustable
# via env var sin recompilar (útil para bajarla en producción durante
# operativos si hace falta).
SYNC_STALE_INTERVAL_MIN: int = int(os.getenv("SYNC_STALE_INTERVAL_MIN", "20"))

# Un device se considera "viejo" si su ``last_sync_at`` (más viejo de los
# 3 scopes core) es mayor a este umbral. Elegido en 7 min para el
# refresh reactivo on-open: valores más chicos disparan refresh cada
# vez que el usuario vuelve al dashboard, valores más grandes muestran
# data más vieja al abrir. Usado también como default por el scheduler
# antes de decidir si vale la pena encolar.
SYNC_STALE_THRESHOLD_S: int = int(os.getenv("SYNC_STALE_THRESHOLD_S", "420"))

# Delay entre tareas encoladas en una misma corrida del scheduler.
# Suaviza el bump de tareas en el broker Celery. El semáforo global de
# sesiones SSH (``RedisCoordinator.adquirir_slot``) igual pone el techo
# real; esto solo evita picos innecesarios en el broker.
SYNC_STALE_STAGGER_S: int = int(os.getenv("SYNC_STALE_STAGGER_S", "3"))


@celery_app.task(name="ansiauth.orquestador.ejecutar")
def ejecutar_task(recurso_dict: dict, tipo_recurso: str, device_name: str, actor: str, job_id: str) -> None:
    from app.composition import job_repository, orquestador
    from app.models.svi import SVI
    from app.models.port import Puerto
    from app.models.vlan import VLAN
    from app.models.global_config import GlobalConfig

    _TIPOS = {"vlan": VLAN, "puerto": Puerto, "svi": SVI, "global_config": GlobalConfig}
    recurso = _TIPOS[tipo_recurso](**recurso_dict)
    job = job_repository.get(job_id)
    orquestador.ejecutar(recurso, device_name, actor, job)


@celery_app.task(name="ansiauth.orquestador.ejecutar_lote")
def ejecutar_lote_task(
    recursos_dict: list[dict], tipo_recurso: str, device_name: str, actor: str, job_id: str,
) -> None:
    """Contraparte de ``ejecutar_task`` para batching (ver
    ``GroupOperationRunner.encolar_lote()``/``Orquestador.ejecutar_lote()``)
    -- misma reconstrucción de tipo, aplicada a una lista en vez de 1 solo
    recurso."""
    from app.composition import job_repository, orquestador
    from app.models.svi import SVI
    from app.models.port import Puerto

    _TIPOS = {"puerto": Puerto, "svi": SVI}
    cls = _TIPOS[tipo_recurso]
    recursos = [cls(**d) for d in recursos_dict]
    job = job_repository.get(job_id)
    orquestador.ejecutar_lote(recursos, device_name, actor, job)


@celery_app.task(name="ansiauth.orquestador.retry_rollback")
def retry_rollback_task(original_job_id: str, new_job_id: str, actor: str) -> None:
    """Ejecuta ``Orquestador.retry_rollback`` en background -- disparado
    por ``POST /jobs/{id}/retry-rollback`` para recuperar un job cuyo
    rollback original falló (``rollback_success=false``). El
    ``original_job`` se lee solo para su ``pre_state``; el resultado se
    escribe sobre el ``new_job`` (creado por el endpoint) que también
    quedó en ``pending`` a la espera de esta task."""
    from app.composition import job_repository, orquestador

    original = job_repository.get(original_job_id)
    new_job = job_repository.get(new_job_id)
    if original is None or new_job is None:
        logger.warning(
            "retry_rollback_task: original=%s o new_job=%s no existe -- "
            "saltando (probablemente eliminado post-encolar).",
            original_job_id, new_job_id,
        )
        return
    orquestador.retry_rollback(original, new_job, actor)


@celery_app.task(name="ansiauth.device.sync")
def sync_device_task(device_name: str, scope: str) -> None:
    """Sincroniza la caché en DB (``device_vlans`` / ``device_ports`` /
    ``device_svis``) con el estado en vivo del equipo.
    ``scope`` ∈ ``{"vlans", "ports", "svis", "global_config", "arp_mac",
    "logs", "all"}``.

    Disparado por:
    * ``Inventory.register()`` con ``scope="all"`` -- alta de device
      (fire-and-forget, el POST vuelve al toque).
    * ``POST /devices/{name}/{vlans,ports,svis}/refresh`` --
      refresh manual granular desde la UI.
    * ``Orquestador`` al completar OK un job -- coherencia post-escritura.
    * ``sync_stale_devices_task`` (Celery Beat) sobre devices con sync
      "viejo".
    * ``POST /dashboard/refresh`` (refresh reactivo al abrir un
      dashboard, filtrado por staleness).

    ``"all"`` corre los 3 scopes CORE (vlans + ports + svis)
    secuencialmente; ``global_config`` quedó afuera del combo (queda
    on-demand con su propio botón, para no traer running-configs
    completos en el flujo automático).

    Si uno falla, los demás igual corren (son independientes -- que las
    VLANs no se puedan leer no es razón para no leer los puertos ni las
    interfaces). Si alguno falló, la task termina en error para que
    Celery/observabilidad lo vean; los detalles ya quedaron persistidos
    en ``devices.{vlans,ports,svis}_sync_error`` por el propio servicio,
    así que el frontend los muestra sin depender del resultado del task.
    """
    from app.composition import device_repository, device_sync_service, redis_coordinator

    # Coalescing: liberá la marca "pending" en cuanto el task arranca --
    # un pedido posterior que llegue mientras corre este SÍ debe poder
    # encolar otra task (su lectura no incluiría cambios post-arranque).
    redis_coordinator.limpiar_sync_pendiente(device_name, scope)

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
    elif scope == "svis":
        device_sync_service.sync_svis(device)
    elif scope == "global_config":
        device_sync_service.sync_global_config(device)
    elif scope == "arp_mac":
        device_sync_service.sync_arp_mac(device)
    elif scope == "logs":
        device_sync_service.sync_logs(device)
    elif scope == "all":
        # global_config quedó afuera del combo por Decisión 3 de
        # docs/SSH_REFRESH_PLAN.md -- se refresca on-demand con su propio
        # botón, para no arrastrar running-configs completos en el flujo
        # automático (scheduler + reactive). ARP/MAC y logs también
        # afuera por el mismo motivo (siempre lo estuvieron).
        #
        # sync_core corre los 3 reads (vlans + ports + svis) en 1 sola
        # sesión SSH en Cisco / 2 en Huawei, contra las 3-4 sesiones que
        # las 3 llamadas individuales generaban. La política de fallos
        # parciales (persistir lo que se leyó OK, marcar error donde no)
        # queda igual que antes.
        device_sync_service.sync_core(device)
    else:
        raise ValueError(
            f"sync_device_task: scope inválido {scope!r} "
            "(esperado: vlans / ports / svis / global_config / arp_mac / logs / all)"
        )


def _encolar_sync_si_no_pendiente(device_name: str, scope: str, countdown: int = 0) -> bool:
    """Encola ``sync_device_task(device_name, scope)`` sólo si no hay ya
    otra pending (coalescing). Devuelve ``True`` si encoló, ``False`` si
    la saltó por duplicado.

    Compartido por el scheduler (``sync_stale_devices_task``) y el
    endpoint reactive (``POST /dashboard/refresh``) -- ambos aplican la
    misma política antes de encolar.
    """
    from app.composition import redis_coordinator

    if redis_coordinator.hay_sync_pendiente(device_name, scope):
        logger.debug(
            "sync coalescing: skip enqueue device=%s scope=%s (already pending)",
            device_name, scope,
        )
        return False
    redis_coordinator.marcar_sync_pendiente(device_name, scope)
    sync_device_task.apply_async(args=[device_name, scope], countdown=countdown)
    return True


@celery_app.task(name="ansiauth.device.sync_stale")
def sync_stale_devices_task() -> None:
    """Barrido periódico (Celery Beat, cada ``SYNC_STALE_INTERVAL_MIN``
    min) que refresca devices cuya última sync core (vlans/ports/svis)
    es más vieja que ``SYNC_STALE_THRESHOLD_S``.

    * Prioriza los devices más viejos primero (los que llevan más tiempo
      sin actualizarse).
    * Aplica coalescing: si un device ya tiene un ``sync_device_task``
      pending, se saltea.
    * Staggering: encola con ``countdown = i * SYNC_STALE_STAGGER_S`` para
      no meter todas las tareas al broker de golpe. El semáforo global
      (``RedisCoordinator.adquirir_slot``) es la protección real contra
      saturación SSH; el staggering solo evita el burst en la cola.

    Reemplaza el patrón de "click de refresh global manual" del
    dashboard: en vez de que el usuario dispare N devices × 3 scopes en
    paralelo, un scheduler backend recorre el inventory de forma
    controlada y predecible.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, or_

    from app.db.models import DeviceModel
    from app.db.session import get_session

    threshold = datetime.now(timezone.utc) - timedelta(seconds=SYNC_STALE_THRESHOLD_S)

    with get_session() as session:
        # ORDER BY el "más viejo de los 3 sync core" ASC -- devices que
        # nunca se sincronizaron (NULL) aparecen primero por NULLS FIRST
        # implícito de la mayoría de dialectos SQL. Aplicamos el filtro
        # de staleness usando el mismo COALESCE de "más viejo entre los
        # 3 sincronizados".
        oldest_sync = func.coalesce(
            func.least(
                func.coalesce(DeviceModel.vlans_synced_at, datetime.min.replace(tzinfo=timezone.utc)),
                func.coalesce(DeviceModel.ports_synced_at, datetime.min.replace(tzinfo=timezone.utc)),
                func.coalesce(DeviceModel.svis_synced_at, datetime.min.replace(tzinfo=timezone.utc)),
            ),
            datetime.min.replace(tzinfo=timezone.utc),
        )
        rows = (
            session.query(DeviceModel.name)
            .filter(
                or_(
                    DeviceModel.vlans_synced_at.is_(None),
                    DeviceModel.ports_synced_at.is_(None),
                    DeviceModel.svis_synced_at.is_(None),
                    oldest_sync < threshold,
                )
            )
            .order_by(oldest_sync.asc())
            .all()
        )
        device_names = [name for (name,) in rows]

    if not device_names:
        logger.debug("sync_stale_devices_task: nothing to refresh")
        return

    encolados = 0
    for i, name in enumerate(device_names):
        countdown = i * SYNC_STALE_STAGGER_S
        if _encolar_sync_si_no_pendiente(name, "all", countdown=countdown):
            encolados += 1
    logger.info(
        "sync_stale_devices_task: candidates=%d enqueued=%d skipped_coalesced=%d",
        len(device_names), encolados, len(device_names) - encolados,
    )
