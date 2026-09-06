"""DeviceSyncService — sincroniza el estado observable de un device
(VLANs, ports) con la caché en DB. Es el único punto donde se traduce
"getter del driver contra el equipo" → "upsert en Repository[T]" +
timestamp/error en DeviceModel.

Invocado por:
* ``sync_device_task`` (Celery, tasks.py) — path normal: alta de device,
  endpoint de refresh manual, hook post-escritura exitosa.
* En tests o scripts ad-hoc que quieran poblar la caché sin worker.

Semántica de fallo: si el lock o el getter fallan, se guarda el mensaje
en ``devices.{vlans,ports}_sync_error`` y se re-lanza la excepción. No
se vacía la tabla ni se actualiza el ``synced_at``, así que la UI sigue
mostrando la última data conocida + banner de error (last sync failed).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from sqlalchemy import update

from app.db.models import DeviceModel
from app.db.session import get_session

if TYPE_CHECKING:
    from app.models.device import Device

logger = logging.getLogger(__name__)

# Coherente con el rate limiter (MAX_JOBS_PER_WINDOW=30/60s): si otra
# operación tiene el device, esperar 30s es razonable; más allá conviene
# devolver el error, dejar que la próxima refresh reintente y liberar el
# worker Celery en vez de dejarlo colgado.
_LOCK_TIMEOUT_S = 30.0
# El error se persiste como Text en devices.{vlans,ports}_sync_error. Un
# traceback crudo puede tener miles de líneas -- truncar acá evita inflar
# la fila y romper cachés/serialización del schema.
_MAX_ERROR_LEN = 2000


class DeviceSyncService:
    def __init__(self, vlan_repo, puerto_repo, svi_repo, global_config_repo, coordinator, arp_mac_repo, device_logs_repo):
        self._vlans = vlan_repo                        # Repository[VLAN]
        self._ports = puerto_repo                      # Repository[Puerto]
        self._interfaces = svi_repo        # Repository[SVI]
        self._global_config = global_config_repo        # Repository[GlobalConfig]
        self._coordinator = coordinator                # RedisCoordinator
        self._arp_mac = arp_mac_repo                    # Repository[ArpMacTables]
        self._device_logs = device_logs_repo            # Repository[DeviceLogs]

    def sync_vlans(self, device: "Device") -> None:
        """Full-refresh de las VLANs de *device* desde el equipo hacia
        ``device_vlans``. Reemplaza la lista completa (agrega nuevas,
        borra las que ya no existen). En éxito actualiza
        ``devices.vlans_synced_at`` y limpia ``vlans_sync_error``; en
        fallo guarda ``vlans_sync_error`` y NO toca ``vlans_synced_at``
        ni la tabla ``device_vlans``.
        """
        try:
            with self._coordinator.bloquear(device.name, timeout=_LOCK_TIMEOUT_S):
                nuevos = device.driver.get_vlans(device, device.password)
        except Exception as exc:
            logger.exception("sync_vlans failed device=%s", device.name)
            self._marcar_error(device.name, "vlans_sync_error", str(exc))
            raise

        # Diff: borrar las que ya no están, upsert de todas las nuevas.
        # add() ya es upsert (session.merge), así que las que ya existían
        # pero cambiaron de nombre quedan actualizadas sin caso especial.
        # remove() explícito para las que desaparecieron del equipo -- si
        # sólo agregara, la fila vieja quedaría stale para siempre.
        existentes = self._vlans.list(device=device.name)
        existentes_ids = {v.vlan_id for v in existentes}
        nuevos_ids = {v.vlan_id for v in nuevos}
        for vlan_id in existentes_ids - nuevos_ids:
            self._vlans.remove((vlan_id, device.name))
        for v in nuevos:
            # Los parsers (services/parsers/vlan_parser.py) construyen VLAN
            # sin setear device -- histórico, el único caller previo era
            # _leer_vlans_en_vivo() que no persistía. Al persistir sí hace
            # falta: (vlan_id, device) es la PK compuesta.
            v.device = device.name
            self._vlans.add(v)

        self._marcar_ok(device.name, "vlans_synced_at", "vlans_sync_error")
        logger.info(
            "sync_vlans OK device=%s persisted=%d removed=%d",
            device.name, len(nuevos), len(existentes_ids - nuevos_ids),
        )

    def sync_ports(self, device: "Device") -> None:
        """Full-refresh de los puertos de *device* desde el equipo hacia
        ``device_ports``. Misma semántica que ``sync_vlans``: reemplaza
        la lista completa y no vacía la tabla en fallo.
        """
        try:
            with self._coordinator.bloquear(device.name, timeout=_LOCK_TIMEOUT_S):
                nuevos = device.driver.list_ports(device, device.password)
        except Exception as exc:
            logger.exception("sync_ports failed device=%s", device.name)
            self._marcar_error(device.name, "ports_sync_error", str(exc))
            raise

        existentes = self._ports.list(device=device.name)
        existentes_ifs = {p.interface for p in existentes}
        nuevos_ifs = {p.interface for p in nuevos}
        for interface in existentes_ifs - nuevos_ifs:
            self._ports.remove((interface, device.name))
        for p in nuevos:
            p.device = device.name
            self._ports.add(p)

        self._marcar_ok(device.name, "ports_synced_at", "ports_sync_error")
        logger.info(
            "sync_ports OK device=%s persisted=%d removed=%d",
            device.name, len(nuevos), len(existentes_ifs - nuevos_ifs),
        )

    def sync_svis(self, device: "Device") -> None:
        """Full-refresh de las SVIs de *device* desde el
        equipo hacia ``device_svis``. Misma semántica que
        ``sync_vlans``/``sync_ports``: reemplaza la lista completa y no
        vacía la tabla en fallo.
        """
        try:
            with self._coordinator.bloquear(device.name, timeout=_LOCK_TIMEOUT_S):
                nuevas = device.driver.get_svis(device, device.password)
        except Exception as exc:
            logger.exception("sync_svis failed device=%s", device.name)
            self._marcar_error(device.name, "svis_sync_error", str(exc))
            raise

        existentes = self._interfaces.list(device=device.name)
        existentes_ids = {i.vlan_id for i in existentes}
        nuevas_ids = {i.vlan_id for i in nuevas}
        for vlan_id in existentes_ids - nuevas_ids:
            self._interfaces.remove((vlan_id, device.name))
        for i in nuevas:
            i.device = device.name
            self._interfaces.add(i)

        self._marcar_ok(device.name, "svis_synced_at", "svis_sync_error")
        logger.info(
            "sync_svis OK device=%s persisted=%d removed=%d",
            device.name, len(nuevas), len(existentes_ids - nuevas_ids),
        )

    def sync_global_config(self, device: "Device") -> None:
        """Full-refresh de la configuración global de *device* hacia
        ``device_global_config``. A diferencia de VLAN/Puerto/SVI es un
        singleton (1 fila por device, no una colección con diff de
        agregados/removidos) -- ``Repository.add()`` ya es upsert
        (``session.merge()``), así que alcanza con sobreescribir la fila."""
        try:
            with self._coordinator.bloquear(device.name, timeout=_LOCK_TIMEOUT_S):
                config = device.driver.get_global_config(device, device.password)
        except Exception as exc:
            logger.exception("sync_global_config failed device=%s", device.name)
            self._marcar_error(device.name, "global_config_sync_error", str(exc))
            raise

        config.device = device.name
        self._global_config.add(config)

        self._marcar_ok(device.name, "global_config_synced_at", "global_config_sync_error")
        logger.info("sync_global_config OK device=%s", device.name)

    def sync_arp_mac(self, device: "Device") -> None:
        """Full-refresh de las tablas ARP/MAC de *device* hacia
        ``device_arp_mac`` -- scope de sync propio, separado de
        ``global_config`` a pedido del usuario: no hace falta para
        ninguna escritura (no pasa por ``reconciliar()`` de nada) y puede
        traer muchísima info, así que no forma parte de ``"all"`` (alta de
        device) ni del sync general -- solo corre cuando alguien pide
        explícitamente ``POST .../arp-mac/refresh``. Singleton igual que
        ``sync_global_config`` (1 fila por device, upsert directo)."""
        from app.models.arp_mac import ArpMacTables

        try:
            with self._coordinator.bloquear(device.name, timeout=_LOCK_TIMEOUT_S):
                arp_table = device.driver.get_arp_table(device, device.password)
                mac_table = device.driver.get_mac_table(device, device.password)
        except Exception as exc:
            logger.exception("sync_arp_mac failed device=%s", device.name)
            self._marcar_error(device.name, "arp_mac_sync_error", str(exc))
            raise

        self._arp_mac.add(ArpMacTables(device=device.name, arp_table=arp_table, mac_table=mac_table))

        self._marcar_ok(device.name, "arp_mac_synced_at", "arp_mac_sync_error")
        logger.info("sync_arp_mac OK device=%s", device.name)

    def sync_logs(self, device: "Device") -> None:
        """Full-refresh del log buffer local de *device* hacia
        ``device_logs`` -- mismo criterio que ``sync_arp_mac()`` (scope
        propio, afuera de ``"all"``, pedido por el usuario "de la misma
        forma que las tablas mac y arp")."""
        from app.models.device_logs import DeviceLogs

        try:
            with self._coordinator.bloquear(device.name, timeout=_LOCK_TIMEOUT_S):
                log_output = device.driver.get_log_buffer(device, device.password)
        except Exception as exc:
            logger.exception("sync_logs failed device=%s", device.name)
            self._marcar_error(device.name, "logs_sync_error", str(exc))
            raise

        self._device_logs.add(DeviceLogs(device=device.name, log_output=log_output))

        self._marcar_ok(device.name, "logs_synced_at", "logs_sync_error")
        logger.info("sync_logs OK device=%s", device.name)

    def metadata(
        self, device_name: str, scope: str,
    ) -> tuple[Optional[datetime], Optional[str]]:
        """Return ``(synced_at, sync_error)`` para *scope* ∈
        {"vlans","ports","svis","global_config","arp_mac","logs"}.

        Consultado por los GET cache-first para poblar el envelope
        ``{data, synced_at, sync_error, sync_in_progress}`` sin que el
        endpoint tenga que hacer una query manual al ``DeviceModel``.
        Si el device no existe devuelve ``(None, None)`` -- el endpoint
        ya validó la existencia con ``require_device()`` antes de llegar
        acá, así que ese caso no debería ocurrir en el camino normal.
        """
        cols = {
            "vlans": (DeviceModel.vlans_synced_at, DeviceModel.vlans_sync_error),
            "ports": (DeviceModel.ports_synced_at, DeviceModel.ports_sync_error),
            "svis": (
                DeviceModel.svis_synced_at,
                DeviceModel.svis_sync_error,
            ),
            "global_config": (
                DeviceModel.global_config_synced_at,
                DeviceModel.global_config_sync_error,
            ),
            "arp_mac": (
                DeviceModel.arp_mac_synced_at,
                DeviceModel.arp_mac_sync_error,
            ),
            "logs": (
                DeviceModel.logs_synced_at,
                DeviceModel.logs_sync_error,
            ),
        }
        if scope not in cols:
            raise ValueError(
                f"metadata(): scope inválido {scope!r} (esperado: vlans / ports / svis / global_config / arp_mac / logs)"
            )
        col_ts, col_err = cols[scope]
        with get_session() as session:
            row = (
                session.query(col_ts, col_err)
                .filter(DeviceModel.name == device_name)
                .first()
            )
            return (row[0], row[1]) if row else (None, None)

    def _marcar_ok(self, device_name: str, ts_field: str, err_field: str) -> None:
        now = datetime.now(timezone.utc)
        with get_session() as session:
            session.execute(
                update(DeviceModel)
                .where(DeviceModel.name == device_name)
                .values(**{ts_field: now, err_field: None})
            )

    def _marcar_error(self, device_name: str, err_field: str, mensaje: str) -> None:
        mensaje = mensaje[:_MAX_ERROR_LEN]
        with get_session() as session:
            session.execute(
                update(DeviceModel)
                .where(DeviceModel.name == device_name)
                .values(**{err_field: mensaje})
            )
