from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import DeviceExecutionError, ValidationError
from app.core.response import ok
from app.core.scope import authorize_device, obtener_scope, require_authenticated, require_device
from app.models.global_config import GlobalConfig
from app.models.visibility_scope import VisibilityScope
from app.schemas.device_sync import SyncedResource
from app.schemas.global_config import (
    GlobalConfigDnsInfo,
    GlobalConfigDnsRemoveRequest,
    GlobalConfigDnsRequest,
    GlobalConfigHostnameUpdateRequest,
    GlobalConfigLoggingInfo,
    GlobalConfigLogServerAddRequest,
    GlobalConfigLogServerRemoveRequest,
    GlobalConfigNtpAddRequest,
    GlobalConfigNtpInfo,
    GlobalConfigNtpRemoveRequest,
    GlobalConfigRead,
    GlobalConfigRunningConfigRead,
    GlobalConfigRouteAddRequest,
    GlobalConfigSnmpInfo,
    GlobalConfigSnmpUpdateRequest,
    GlobalConfigVersionRead,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# ARP/MAC `include` -- filtro de sub-string sobre las filas ya cacheadas
# (ver ``get_global_config_arp``/``_mac``). Ya NO viaja a una línea de
# comando sobre SSH (ARP/MAC se agregaron al cache, dejaron de leerse en
# vivo por request) -- el charset conservador se mantiene igual por
# higiene de API, no por necesidad de evitar inyección de comandos.
_ARP_MAC_INCLUDE_RE = r"^[A-Za-z0-9:./_-]{1,64}$"


def _filtrar_entradas_arp_mac(entries: "list[dict] | None", include: "str | None") -> "list[dict] | None":
    """Sub-string case-insensitive contra todos los valores de cada fila
    (mismo espíritu que el `| include` del device que reemplaza, pero
    corriendo en Python sobre datos ya cacheados en vez de en el device)."""
    if entries is None or not include:
        return entries
    needle = include.lower()
    return [
        entry for entry in entries
        if needle in " ".join(str(v) for v in entry.values() if v is not None).lower()
    ]


def _authz_device(
    scope: VisibilityScope, device_name: str, *, min_role: str, device: "Device | None" = None,
) -> None:
    """Mismo criterio que ``api/svis.py``'s ``_authz_device``."""
    resolved = (device.site_id, device.device_group_id) if device is not None else None
    authorize_device(scope, device_name, "global_config_device_op", min_role, resolved=resolved)


def _require_driver_with(device: "Device", method_name: str):
    """Mismo criterio que ``api/svis.py``'s ``_require_driver_with`` -- 501
    ``VENDOR_NOT_SUPPORTED`` si el driver concreto no sobreescribe
    *method_name* (todavía stub en ``VendorDriver``)."""
    from fastapi import HTTPException

    from app.services.vendors.base import VendorDriver

    driver = device.driver
    if getattr(type(driver), method_name) is getattr(VendorDriver, method_name):
        logger.info(
            "GlobalConfig %s unimplemented on driver=%s for device=%s",
            method_name, type(driver).__name__, device.name,
        )
        raise HTTPException(
            status_code=501,
            detail={
                "error_code": "VENDOR_NOT_SUPPORTED",
                "message": "This global configuration operation is not yet supported for this vendor.",
            },
        )
    return driver


@router.get(
    "/",
    summary="Get global configuration",
    description=(
        "Retrieve the device's configured settings — hostname, SNMP, "
        "NTP/DNS/log servers, routing table and ACL names "
        "(RF-GLOBAL-01/02/03/04). The full raw config dump and the device "
        "version each live in their own endpoint (`GET .../running-config`, "
        "`GET .../version`) — kept out of here since they're the heaviest "
        "part of the response and conceptually distinct from these "
        "individual settings. Cache-first — reads from the synced cache "
        "(populated on device registration, manual refresh, or after a "
        "write), not live from the equipment. Requires observer role or "
        "higher; site-scoped users may only query devices in their "
        "allowed sites."
    ),
)
def get_global_config(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_sync_service, global_config_repository, redis_coordinator

    _authz_device(scope, name, min_role="observer")
    dev = require_device(name)
    config = global_config_repository.get(name)
    payload = {
        "device": dev.name,
        "vendor": dev.vendor,
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
    synced_at, sync_error = device_sync_service.metadata(name, "global_config")
    envelope = SyncedResource(
        data=payload,
        synced_at=synced_at,
        sync_error=sync_error,
        sync_in_progress=redis_coordinator.esta_ocupado(name),
    ).model_dump(mode="json")
    return ok(envelope)


@router.get(
    "/version",
    summary="Get device version",
    description=(
        "Retrieve the device's version info ('show version'/'display "
        "version' — RF-GLOBAL-01, the 'y/o versión' half of the use "
        "case, split into its own endpoint from the config dump). "
        "Cache-first, same underlying sync as `GET /` — this doesn't "
        "trigger a separate read. Requires observer role or higher."
    ),
)
def get_global_config_version(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_sync_service, global_config_repository, redis_coordinator
    from app.services.parsers._common import parse_version_info

    _authz_device(scope, name, min_role="observer")
    dev = require_device(name)
    config = global_config_repository.get(name)
    info = parse_version_info(config.device_version if config and config.device_version else "")
    payload = {
        "device": dev.name,
        "vendor": dev.vendor,
        **GlobalConfigVersionRead(**info).model_dump(),
    }
    synced_at, sync_error = device_sync_service.metadata(name, "global_config")
    envelope = SyncedResource(
        data=payload,
        synced_at=synced_at,
        sync_error=sync_error,
        sync_in_progress=redis_coordinator.esta_ocupado(name),
    ).model_dump(mode="json")
    return ok(envelope)


@router.get(
    "/running-config",
    summary="Get the device's full running-config",
    description=(
        "Retrieve the device's full configuration dump ('show "
        "running-config'/'display current-configuration') as a list of "
        "lines, with the leading metadata lines stripped (RF-GLOBAL-01, "
        "the 'consultar configuración general' half of the use case — "
        "kept in its own endpoint since it's the heaviest part of the "
        "response and conceptually distinct from `GET /`'s individual "
        "settings). Cache-first, same underlying sync as `GET /` — this "
        "doesn't trigger a separate read. Requires observer role or higher."
    ),
)
def get_global_config_running_config(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_sync_service, global_config_repository, redis_coordinator

    _authz_device(scope, name, min_role="observer")
    dev = require_device(name)
    config = global_config_repository.get(name)
    payload = {
        "device": dev.name,
        "vendor": dev.vendor,
        **GlobalConfigRunningConfigRead(
            running_config=config.running_config.splitlines() if config and config.running_config else None,
        ).model_dump(),
    }
    synced_at, sync_error = device_sync_service.metadata(name, "global_config")
    envelope = SyncedResource(
        data=payload,
        synced_at=synced_at,
        sync_error=sync_error,
        sync_in_progress=redis_coordinator.esta_ocupado(name),
    ).model_dump(mode="json")
    return ok(envelope)


@router.patch(
    "/hostname",
    status_code=202,
    summary="Set device hostname",
    description=(
        "Modify the device's hostname (RF-GLOBAL-08). No-op when the "
        "requested hostname already matches the device's current "
        "hostname. Executed asynchronously: the response carries a "
        "`group_job_id` and per-device job entry the frontend can poll "
        "via `GET /api/v1/jobs/{job_id}` and `GET /api/v1/group-jobs/{id}`. "
        "Requires admin role or higher — a hostname change affects the "
        "whole device, not a single sub-resource."
    ),
)
def set_global_config_hostname(
    name: str,
    data: GlobalConfigHostnameUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = GlobalConfig(hostname=data.hostname)
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="admin", device=dev)
    _require_driver_with(dev, "set_hostname")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.post(
    "/routes",
    status_code=202,
    summary="Add a static route",
    description=(
        "Add a static route (RF-GLOBAL-06). `destination` is normalized to "
        "its network address before comparing/applying. No-op if the exact "
        "same destination+next-hop already exists. If the destination "
        "already exists with a DIFFERENT next-hop, nothing is applied and "
        "the response carries `accion=\"ruta_ya_existe\"` (SRS alternative "
        "course: notify instead of silently overwriting). Executed "
        "asynchronously, same job-polling shape as the other global-config "
        "writes. Requires admin role or higher."
    ),
)
def add_global_config_route(
    name: str,
    data: GlobalConfigRouteAddRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = GlobalConfig(route_add=data.model_dump())
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="admin", device=dev)
    _require_driver_with(dev, "set_route")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.delete(
    "/routes",
    status_code=202,
    summary="Remove a static route",
    description=(
        "Remove a static route (RF-GLOBAL-06). Same body shape as `POST "
        "/routes` (`destination`+`next_hop`, both required to identify the "
        "exact route). No-op if no route with that exact destination+"
        "next-hop exists. Executed asynchronously, same job-polling shape "
        "as the other global-config writes. Requires admin role or higher."
    ),
)
def remove_global_config_route(
    name: str,
    data: GlobalConfigRouteAddRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = GlobalConfig(route_remove=data.model_dump())
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="admin", device=dev)
    _require_driver_with(dev, "remove_route")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.post(
    "/ntp",
    status_code=202,
    summary="Add an NTP server",
    description=(
        "Add an NTP server (RF-GLOBAL-09, split into its own endpoint). "
        "`prefer` only has a confirmed effect on Cisco. No no-op detection "
        "— NTP server reads aren't implemented, the command is always "
        "sent. Executed asynchronously, same job-polling shape as the "
        "other global-config writes. Requires admin role or higher."
    ),
)
def add_global_config_ntp(
    name: str,
    data: GlobalConfigNtpAddRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = GlobalConfig(ntp_server_add=data.model_dump(exclude_none=True))
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="admin", device=dev)
    _require_driver_with(dev, "add_ntp_server")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.delete(
    "/ntp",
    status_code=202,
    summary="Remove an NTP server",
    description=(
        "Remove an NTP server (RF-GLOBAL-09). No no-op detection, same "
        "reason as the add endpoint. Executed asynchronously, same "
        "job-polling shape as the other global-config writes. Requires "
        "admin role or higher."
    ),
)
def remove_global_config_ntp(
    name: str,
    data: GlobalConfigNtpRemoveRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = GlobalConfig(ntp_server_remove=data.model_dump())
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="admin", device=dev)
    _require_driver_with(dev, "remove_ntp_server")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.post(
    "/dns",
    status_code=202,
    summary="Add a DNS server or set the domain name",
    description=(
        "Add a DNS server OR set the device's domain-name (RF-GLOBAL-09, "
        "split into its own endpoint) — exactly one of `server`/"
        "`domain_name` per request. No no-op detection — neither is read "
        "back today. Executed asynchronously, same job-polling shape as "
        "the other global-config writes. Requires admin role or higher."
    ),
)
def add_global_config_dns(
    name: str,
    data: GlobalConfigDnsRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    if data.server is not None:
        entidad = GlobalConfig(dns_server_add={"server": data.server})
        driver_method = "add_dns_server"
    else:
        entidad = GlobalConfig(dns_domain_set=data.domain_name)
        driver_method = "set_dns_domain"
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="admin", device=dev)
    _require_driver_with(dev, driver_method)

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.delete(
    "/dns",
    status_code=202,
    summary="Remove a DNS server",
    description=(
        "Remove a DNS server (RF-GLOBAL-09). No no-op detection. Executed "
        "asynchronously, same job-polling shape as the other "
        "global-config writes. Requires admin role or higher."
    ),
)
def remove_global_config_dns(
    name: str,
    data: GlobalConfigDnsRemoveRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = GlobalConfig(dns_server_remove={"server": data.server})
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="admin", device=dev)
    _require_driver_with(dev, "remove_dns_server")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.patch(
    "/snmp",
    status_code=202,
    summary="Configure SNMP",
    description=(
        "Configure SNMP: version, community (always read-only), trap-source "
        "interface, and/or trap-host+trap-version (RF-GLOBAL-07). "
        "`trap_host` and `trap_version` must be provided together, and "
        "`trap_host` needs a resolvable community (either in this same "
        "request or already configured on the device). No-op on sub-fields "
        "that already match the device's current state. `trap_source` has "
        "no confirmed effect on Huawei yet; `trap_host` is not yet "
        "supported on Huawei (surfaces as a failed job, see job error). "
        "Executed asynchronously, same job-polling shape as the other "
        "global-config writes. Requires admin role or higher."
    ),
)
def set_global_config_snmp(
    name: str,
    data: GlobalConfigSnmpUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = GlobalConfig(snmp_config=data.model_dump(exclude_none=True))
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="admin", device=dev)
    _require_driver_with(dev, "set_snmp")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.post(
    "/log-servers",
    status_code=202,
    summary="Add a syslog server",
    description=(
        "Add a syslog server, optionally setting the severity level "
        "(RF-GLOBAL-09, split into its own endpoint). `level` is a "
        "device-global setting on both vendors (not per-host), it just "
        "travels in the same request for convenience. No no-op detection "
        "— neither is read back today. Executed asynchronously, same "
        "job-polling shape as the other global-config writes. Requires "
        "admin role or higher."
    ),
)
def add_global_config_log_server(
    name: str,
    data: GlobalConfigLogServerAddRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = GlobalConfig(log_server_add=data.model_dump(exclude_none=True))
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="admin", device=dev)
    _require_driver_with(dev, "add_log_server")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.delete(
    "/log-servers",
    status_code=202,
    summary="Remove a syslog server",
    description=(
        "Remove a syslog server (RF-GLOBAL-09). No no-op detection. "
        "Executed asynchronously, same job-polling shape as the other "
        "global-config writes. Requires admin role or higher."
    ),
)
def remove_global_config_log_server(
    name: str,
    data: GlobalConfigLogServerRemoveRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = GlobalConfig(log_server_remove={"server": data.server})
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="admin", device=dev)
    _require_driver_with(dev, "remove_log_server")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.get(
    "/arp",
    summary="Get ARP table",
    description=(
        "Retrieve the device's ARP table, parsed into structured rows "
        "(`ip`, `mac`, `interface`, `vlan`, `type`, `age` — fields not "
        "reported by a given vendor come back `null`). Cache-first, same "
        "underlying sync as `GET /` — this used to be the only live, "
        "uncached read in this app (the table changes constantly), but "
        "paid the full cost of a new SSH/Ansible session on every request; "
        "now it's read in full during the regular sync and served from "
        "cache like everything else, accepting staleness between syncs in "
        "exchange for an instant response. `include` is optional and now "
        "filters the cached rows in Python (case-insensitive substring "
        "match across all fields) instead of piping through the device. "
        "Requires observer role or higher."
    ),
)
def get_global_config_arp(
    name: str,
    include: "str | None" = Query(default=None, pattern=_ARP_MAC_INCLUDE_RE),
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_sync_service, global_config_repository, redis_coordinator

    _authz_device(scope, name, min_role="observer")
    dev = require_device(name)
    config = global_config_repository.get(name)
    payload = {
        "device": dev.name,
        "vendor": dev.vendor,
        "entries": _filtrar_entradas_arp_mac(config.arp_table if config else None, include),
    }
    synced_at, sync_error = device_sync_service.metadata(name, "global_config")
    envelope = SyncedResource(
        data=payload,
        synced_at=synced_at,
        sync_error=sync_error,
        sync_in_progress=redis_coordinator.esta_ocupado(name),
    ).model_dump(mode="json")
    return ok(envelope)


@router.get(
    "/mac",
    summary="Get MAC address table",
    description=(
        "Retrieve the device's MAC address table, parsed into structured "
        "rows (`mac`, `vlan`, `interface`, `type`). Cache-first, same "
        "criteria as `/arp` (see its description for why this changed from "
        "a live read). `include` filters the cached rows in Python "
        "(case-insensitive substring match across all fields). Requires "
        "observer role or higher."
    ),
)
def get_global_config_mac(
    name: str,
    include: "str | None" = Query(default=None, pattern=_ARP_MAC_INCLUDE_RE),
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_sync_service, global_config_repository, redis_coordinator

    _authz_device(scope, name, min_role="observer")
    dev = require_device(name)
    config = global_config_repository.get(name)
    payload = {
        "device": dev.name,
        "vendor": dev.vendor,
        "entries": _filtrar_entradas_arp_mac(config.mac_table if config else None, include),
    }
    synced_at, sync_error = device_sync_service.metadata(name, "global_config")
    envelope = SyncedResource(
        data=payload,
        synced_at=synced_at,
        sync_error=sync_error,
        sync_in_progress=redis_coordinator.esta_ocupado(name),
    ).model_dump(mode="json")
    return ok(envelope)
