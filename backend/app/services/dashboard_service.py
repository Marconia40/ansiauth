"""DashboardService — agregación server-side para GET /api/v1/dashboard/summary.

Reemplaza el patrón N+1 del frontend (traer lista completa de devices +
un getVlans/getPorts/getSVIs por device) con una única query resuelta
en el backend. Todo el conteo (devices por vendor, ports por estado,
VLANs únicas, jobs por status) se hace acá en 5–7 queries a la DB.

Autorización — política B ("mostrar lo que el user puede ver"): si el
scope pedido existe pero el caller no tiene visibilidad sobre ningún
device dentro, el summary vuelve con contadores en cero, NO se
responde 403. Solo se responde 404 si el site/group/device de la URL
no existe.

VLAN name discrepancy: NO se calcula acá. Se devuelve la lista de
names únicos por VLAN y el frontend chequea ``names.length > 1``.
Decisión de diseño para mantener la lógica que ya tiene el frontend
y no duplicarla.

``global_config`` (SNMP/NTP/DNS/logging/ACLs por device) es una
sección aparte, opt-in vía ``include_global_config`` -- no forma parte
del summary/refresh por default. Reusa el mismo endpoint en vez de un
router/servicio propio (ver ``_resumen_global_config``/
``build_global_config_payload``), pero se mantiene fuera del refresh
automático porque ``global_config`` está excluido del combo ``"all"``
de ``sync_device_task`` a propósito (Decisión 3, ``app/tasks.py``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, or_

from app.db.models import (
    DeviceGlobalConfigModel,
    DeviceGroupModel,
    DeviceModel,
    DevicePortModel,
    DeviceSVIModel,
    DeviceVlanModel,
    JobModel,
)
from app.db.session import get_session
from app.models.visibility_scope import VisibilityScope


def build_global_config_payload(device: str, vendor: "str | None", config) -> dict:
    """Arma el dict wire-format (``GlobalConfigRead`` + device/vendor) a
    partir de un objeto con los campos de ``GlobalConfig``/
    ``DeviceGlobalConfigModel`` (mismos nombres de atributo en ambos --
    domain object y fila ORM son intercambiables acá). ``config`` puede
    ser ``None`` (device sin sync todavía).

    Compartido por ``GET /devices/{name}/global-config/``
    (``api/global_config.py``, pasa el domain object del repository) y
    ``DashboardService._resumen_global_config`` (pasa la fila ORM
    directo, sin pasar por el repository, para poder traer N devices en
    1 sola query -- ver docstring de ``_resumen_global_config``)."""
    from app.schemas.global_config import (
        GlobalConfigDnsInfo,
        GlobalConfigLoggingInfo,
        GlobalConfigNtpInfo,
        GlobalConfigRead,
        GlobalConfigSnmpInfo,
    )

    return {
        "device": device,
        "vendor": vendor,
        **GlobalConfigRead(
            hostname=config.hostname if config else None,
            snmp=GlobalConfigSnmpInfo(
                enabled=config.snmp_enabled if config else None,
                version=config.snmp_version if config else None,
                community=config.snmp_community if config else None,
                permission=config.snmp_permission if config else None,
                trap_hosts=config.snmp_trap_hosts if config else None,
            ),
            ntp=GlobalConfigNtpInfo(servers=config.ntp_servers if config else None),
            dns=GlobalConfigDnsInfo(servers=config.dns_servers if config else None),
            logging=GlobalConfigLoggingInfo(
                servers=config.log_servers if config else None,
                level=config.log_level if config else None,
            ),
            routes=config.routes if config else None,
            acls=config.acls if config else None,
        ).model_dump(),
    }


class DashboardService:
    """Agregación para el endpoint de summary. Sin estado más allá del
    coordinator Redis (necesario para contar sync_in_progress).
    """

    def __init__(self, redis_coordinator):
        self._coordinator = redis_coordinator

    # ── API pública ───────────────────────────────────────────────────────

    def resumir(
        self,
        *,
        scope_kind: str,
        scope_id: Optional[int],
        device_name: Optional[str],
        jobs_days: int,
        visibility_scope: VisibilityScope,
        include_global_config: bool = False,
    ) -> dict:
        """Resolver el scope y devolver el payload completo del dashboard.

        Devuelve un dict con la misma forma que
        ``DashboardSummaryResponse``. Todo el conteo sucede acá; el
        endpoint solo envuelve en ``ok()`` y devuelve.

        No maneja HTTPExceptions — deja que el endpoint (que sabe de
        FastAPI) traduzca el resultado a NotFoundError si hace falta.
        Retorna ``None`` cuando el scope resource no existe (site/group/
        device inexistente) para que el caller responda 404.

        ``include_global_config``: opt-in, default ``False``. Agrega la
        sección ``global_config`` (SNMP/NTP/DNS/logging/ACLs por device)
        al payload. Deliberadamente NO default-on: a diferencia de
        vlans/ports/svis, global_config queda afuera del refresh
        automático/reactivo (ver ``sync_device_task`` scope ``"all"`` en
        ``app/tasks.py`` -- "Decisión 3", trae running-configs completos)
        y no debe pesar en el dashboard normal que sí se abre siempre.
        """
        # 1. Resolver nombres del scope + display name (para el header UI)
        resuelto = self.resolver_devices_del_scope(
            scope_kind, scope_id, device_name, visibility_scope,
        )
        if resuelto is None:
            return None  # 404 al caller
        device_names, scope_display_name = resuelto

        # 2. Agregación por sección — cada método hace 1 query a la DB
        devices = self._resumen_devices(device_names)
        vlans = self._resumen_vlans(device_names)
        ports = self._resumen_ports(device_names)
        svis = self._resumen_svis(device_names)
        jobs = self._resumen_jobs(device_names, jobs_days, visibility_scope)

        payload = {
            "scope": {
                "kind": scope_kind,
                "id": scope_id,
                "name": scope_display_name,
            },
            "generated_at": datetime.now(timezone.utc),
            "devices": devices,
            "vlans": vlans,
            "ports": ports,
            "svis": svis,
            "jobs": jobs,
        }
        if include_global_config:
            payload["global_config"] = self._resumen_global_config(device_names)
        return payload

    # ── Resolver scope → set de device names ──────────────────────────────

    def resolver_devices_del_scope(
        self,
        scope_kind: str,
        scope_id: Optional[int],
        device_name: Optional[str],
        visibility_scope: VisibilityScope,
    ) -> Optional[tuple[set[str], Optional[str]]]:
        """Devuelve ``(device_names, display_name)`` o ``None`` si el scope
        resource no existe.

        ``device_names`` es el subconjunto de nombres visibles al caller
        dentro del scope. Puede ser vacío (política B: caller no ve nada
        en ese scope, pero el scope existe → summary con ceros / refresh
        sin devices a encolar).

        ``display_name`` es opcional: para site/group es el nombre para
        mostrar en el header del frontend; para org es None; para device
        es el mismo device name (redundante pero coherente).

        Público porque tanto ``resumir()`` como el endpoint
        ``POST /dashboard/refresh`` necesitan la misma resolución.
        """
        # Imports lazy para evitar ciclos con composition.py
        from app.composition import (
            device_group_repository,
            device_repository,
            site_repository,
        )

        # Set de nombres visibles al caller (None = system-admin, ve todos)
        visibles = device_repository.nombres_visibles(visibility_scope)

        if scope_kind == "org":
            return self._names_org(visibles), None

        if scope_kind == "site":
            site = site_repository.get(scope_id)
            if site is None:
                return None
            names = self._names_por_site(scope_id, visibles)
            return names, site.name

        if scope_kind == "group":
            group = device_group_repository.get(scope_id)
            if group is None:
                return None
            names = self._names_por_grupo(scope_id, visibles)
            return names, group.name

        if scope_kind == "device":
            # Existencia del device se chequea contra la DB (no contra
            # ``visibles``, porque un device visible-para-otro-user existe
            # igual). Si el device existe pero no es visible al caller,
            # names queda vacío y el summary vuelve en ceros.
            dev = device_repository.get(device_name)
            if dev is None:
                return None
            if visibles is None or device_name in visibles:
                return {device_name}, device_name
            return set(), device_name

        # scope_kind ya viene validado por el pattern del Query en el
        # endpoint; llegar acá sería un bug.
        raise ValueError(f"Scope kind inválido: {scope_kind!r}")

    def _names_org(self, visibles: Optional[set[str]]) -> set[str]:
        if visibles is not None:
            return visibles
        # system-admin: traer todos los nombres
        with get_session() as session:
            return {r[0] for r in session.query(DeviceModel.name).all()}

    def _names_por_site(
        self, site_id: int, visibles: Optional[set[str]],
    ) -> set[str]:
        with get_session() as session:
            q = (
                session.query(DeviceModel.name)
                .join(
                    DeviceGroupModel,
                    DeviceModel.device_group_id == DeviceGroupModel.id,
                )
                .filter(DeviceGroupModel.site_id == site_id)
            )
            names_en_site = {r[0] for r in q.all()}
        if visibles is None:
            return names_en_site
        return names_en_site & visibles

    def _names_por_grupo(
        self, group_id: int, visibles: Optional[set[str]],
    ) -> set[str]:
        with get_session() as session:
            q = session.query(DeviceModel.name).filter(
                DeviceModel.device_group_id == group_id,
            )
            names_en_grupo = {r[0] for r in q.all()}
        if visibles is None:
            return names_en_grupo
        return names_en_grupo & visibles

    # ── Agregadores por sección ──────────────────────────────────────────

    def _resumen_devices(self, device_names: set[str]) -> dict:
        """Cuenta total, por vendor, y consolida metadata de sync
        (último synced_at, cuántos con error, cuántos con lock activo).
        """
        if not device_names:
            return {
                "total": 0,
                "by_vendor": {},
                "sync_errors": 0,
                "last_sync_at": None,
                "sync_in_progress_count": 0,
            }

        with get_session() as session:
            # Total + by_vendor en una sola query.
            rows = (
                session.query(DeviceModel.vendor, func.count(DeviceModel.id))
                .filter(DeviceModel.name.in_(device_names))
                .group_by(DeviceModel.vendor)
                .all()
            )
            by_vendor = {vendor: count for vendor, count in rows}
            total = sum(by_vendor.values())

            # Sync metadata: cuenta de devices con al menos un
            # *_sync_error != null, y el MIN de los tres *_synced_at.
            # Usamos three consultas simples en vez de una monster query
            # con COALESCE por claridad — igual son <1ms por índice.
            sync_error_count = (
                session.query(func.count(DeviceModel.id))
                .filter(DeviceModel.name.in_(device_names))
                .filter(
                    or_(
                        DeviceModel.vlans_sync_error.isnot(None),
                        DeviceModel.ports_sync_error.isnot(None),
                        DeviceModel.svis_sync_error.isnot(None),
                    )
                )
                .scalar() or 0
            )

            # El "último synced_at" del scope es el más VIEJO entre los
            # sincronizados (política existente en frontend/scopeRefresh):
            # muestro el peor caso para que el UI diga "hace X min" con
            # el device más rezagado. Devices sin ningún sync (todos
            # timestamps null) se excluyen.
            last_sync = (
                session.query(
                    func.min(
                        func.coalesce(
                            DeviceModel.vlans_synced_at,
                            DeviceModel.ports_synced_at,
                            DeviceModel.svis_synced_at,
                        )
                    )
                )
                .filter(DeviceModel.name.in_(device_names))
                .scalar()
            )

        # sync_in_progress: 1 chequeo Redis por device (~0.1ms cada uno).
        # Con 500 devices son ~50ms; aceptable para un endpoint que
        # reemplaza 500 requests HTTP.
        in_progress = sum(
            1 for name in device_names if self._coordinator.esta_ocupado(name)
        )

        return {
            "total": total,
            "by_vendor": by_vendor,
            "sync_errors": int(sync_error_count),
            "last_sync_at": last_sync,
            "sync_in_progress_count": in_progress,
        }

    def _resumen_vlans(self, device_names: set[str]) -> dict:
        """Devuelve los IDs únicos + la lista de names únicos por ID.
        El frontend detecta discrepancia con ``len(names) > 1``.
        """
        if not device_names:
            return {"unique_count": 0, "entries": []}

        with get_session() as session:
            # Todas las filas (vlan_id, name) del scope. Con 500 devices
            # × 30 VLANs promedio = 15k filas × ~30B ~= 450KB — perfectly
            # fine para memoria en Python. Si algún día explota se puede
            # switch a un GROUP BY con array_agg (PG-specific).
            rows = (
                session.query(DeviceVlanModel.vlan_id, DeviceVlanModel.name)
                .filter(DeviceVlanModel.device.in_(device_names))
                .all()
            )

        # Agregar en Python: para cada vlan_id, set de names únicos.
        names_por_id: dict[int, set[str]] = {}
        for vlan_id, name in rows:
            names_por_id.setdefault(vlan_id, set()).add(name)

        entries = [
            {"id": vlan_id, "names": sorted(names)}
            for vlan_id, names in sorted(names_por_id.items())
        ]
        return {"unique_count": len(entries), "entries": entries}

    def _resumen_ports(self, device_names: set[str]) -> dict:
        """Contadores de estado de puerto — misma lógica que el
        frontend usaba en ``computeTotals``:

        * shutdown = admin_up == False (independiente de operational)
        * up       = admin_up != False AND operational_up == True
        * down     = admin_up != False AND operational_up == False
        * (unknown = admin_up != False AND operational_up == None) —
          suma al total pero no a ningún bucket

        Un ``CASE WHEN`` con GROUP BY resuelve todo en 1 query.
        """
        if not device_names:
            return {"total": 0, "up": 0, "down": 0, "shutdown": 0}

        with get_session() as session:
            total = (
                session.query(func.count(DevicePortModel.interface))
                .filter(DevicePortModel.device.in_(device_names))
                .scalar() or 0
            )
            shutdown = (
                session.query(func.count(DevicePortModel.interface))
                .filter(DevicePortModel.device.in_(device_names))
                .filter(DevicePortModel.admin_up.is_(False))
                .scalar() or 0
            )
            up = (
                session.query(func.count(DevicePortModel.interface))
                .filter(DevicePortModel.device.in_(device_names))
                .filter(DevicePortModel.admin_up.isnot(False))
                .filter(DevicePortModel.operational_up.is_(True))
                .scalar() or 0
            )
            down = (
                session.query(func.count(DevicePortModel.interface))
                .filter(DevicePortModel.device.in_(device_names))
                .filter(DevicePortModel.admin_up.isnot(False))
                .filter(DevicePortModel.operational_up.is_(False))
                .scalar() or 0
            )
        return {
            "total": int(total),
            "up": int(up),
            "down": int(down),
            "shutdown": int(shutdown),
        }

    def _resumen_svis(self, device_names: set[str]) -> dict:
        """Mismos buckets que ports — misma semántica de admin/operational.
        SVI virtualiza una L3 sobre un VLAN, pero tiene los mismos estados.
        """
        if not device_names:
            return {"total": 0, "up": 0, "down": 0, "shutdown": 0}

        with get_session() as session:
            total = (
                session.query(func.count(DeviceSVIModel.vlan_id))
                .filter(DeviceSVIModel.device.in_(device_names))
                .scalar() or 0
            )
            shutdown = (
                session.query(func.count(DeviceSVIModel.vlan_id))
                .filter(DeviceSVIModel.device.in_(device_names))
                .filter(DeviceSVIModel.admin_up.is_(False))
                .scalar() or 0
            )
            up = (
                session.query(func.count(DeviceSVIModel.vlan_id))
                .filter(DeviceSVIModel.device.in_(device_names))
                .filter(DeviceSVIModel.admin_up.isnot(False))
                .filter(DeviceSVIModel.operational_up.is_(True))
                .scalar() or 0
            )
            down = (
                session.query(func.count(DeviceSVIModel.vlan_id))
                .filter(DeviceSVIModel.device.in_(device_names))
                .filter(DeviceSVIModel.admin_up.isnot(False))
                .filter(DeviceSVIModel.operational_up.is_(False))
                .scalar() or 0
            )
        return {
            "total": int(total),
            "up": int(up),
            "down": int(down),
            "shutdown": int(shutdown),
        }

    def _resumen_jobs(
        self,
        device_names: set[str],
        jobs_days: int,
        visibility_scope: VisibilityScope,
    ) -> dict:
        """Cuenta jobs de la ventana temporal por status +
        rollback_performed. No usa job_repository.query() porque solo
        queremos contar, no traer las filas.
        """
        window_days = jobs_days
        from_date = datetime.now(timezone.utc) - timedelta(days=window_days)

        if not device_names:
            return {
                "window_days": window_days,
                "total": 0,
                "by_status": {},
                "rollback_performed_count": 0,
            }

        with get_session() as session:
            base_filter = and_(
                JobModel.created_at >= from_date,
                JobModel.device.in_(device_names),
            )

            rows = (
                session.query(JobModel.status, func.count(JobModel.id))
                .filter(base_filter)
                .group_by(JobModel.status)
                .all()
            )
            by_status = {status: count for status, count in rows}
            total = sum(by_status.values())

            rollback_count = (
                session.query(func.count(JobModel.id))
                .filter(base_filter)
                .filter(JobModel.rollback_performed.is_(True))
                .scalar() or 0
            )

        return {
            "window_days": window_days,
            "total": total,
            "by_status": by_status,
            "rollback_performed_count": int(rollback_count),
        }

    def _resumen_global_config(self, device_names: set[str]) -> list[dict]:
        """A diferencia de vlans/ports/svis (N filas por device), acá hay
        1 sola fila por device (PK simple en ``device_global_config``) --
        no hace falta agregación, solo traer y armar el payload wire-format
        de cada uno. 2 queries totales (config + metadata de sync), sin
        pasar por ``global_config_repository.get()`` N veces.
        """
        if not device_names:
            return []

        # Columnas explícitas (no el modelo ORM completo) -- mismo criterio
        # que el resto de este archivo (ver _resumen_vlans/_resumen_ports):
        # ``get_session()`` hace commit+close al salir del ``with``, lo que
        # expira las instancias ORM; acceder a sus atributos después
        # (fuera del with) tira DetachedInstanceError. Con tuplas de
        # columnas planas no hay ese problema.
        with get_session() as session:
            config_rows = (
                session.query(
                    DeviceGlobalConfigModel.device,
                    DeviceGlobalConfigModel.hostname,
                    DeviceGlobalConfigModel.snmp_enabled,
                    DeviceGlobalConfigModel.snmp_version,
                    DeviceGlobalConfigModel.snmp_community,
                    DeviceGlobalConfigModel.snmp_permission,
                    DeviceGlobalConfigModel.snmp_trap_hosts,
                    DeviceGlobalConfigModel.ntp_servers,
                    DeviceGlobalConfigModel.dns_servers,
                    DeviceGlobalConfigModel.log_servers,
                    DeviceGlobalConfigModel.log_level,
                    DeviceGlobalConfigModel.routes,
                    DeviceGlobalConfigModel.acls,
                )
                .filter(DeviceGlobalConfigModel.device.in_(device_names))
                .all()
            )
            meta_rows = (
                session.query(
                    DeviceModel.name,
                    DeviceModel.vendor,
                    DeviceModel.global_config_synced_at,
                    DeviceModel.global_config_sync_error,
                )
                .filter(DeviceModel.name.in_(device_names))
                .all()
            )

        configs_by_device = {row.device: row for row in config_rows}

        entries = []
        for name, vendor, synced_at, sync_error in meta_rows:
            entry = build_global_config_payload(name, vendor, configs_by_device.get(name))
            entry["synced_at"] = synced_at
            entry["sync_error"] = sync_error
            entry["sync_in_progress"] = self._coordinator.esta_ocupado(name)
            entries.append(entry)
        return sorted(entries, key=lambda e: e["device"])
