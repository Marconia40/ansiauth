from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.core.exceptions import ValidationError
from app.core.response import ok
from app.core.scope import authorize_device, obtener_scope, require_authenticated, require_device
from app.models.global_config import GlobalConfig
from app.models.visibility_scope import VisibilityScope
from app.schemas.device_sync import SyncedResource
from app.schemas.global_config import (
    GlobalConfigHostnameUpdateRequest,
    GlobalConfigLogServersUpdateRequest,
    GlobalConfigRead,
    GlobalConfigRouteAddRequest,
    GlobalConfigSnmpUpdateRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()


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
        "Retrieve device-wide configuration: version, hostname, SNMP "
        "status, routing table and ACL names (RF-GLOBAL-01/02/03/04). "
        "Cache-first — reads from the synced cache (populated on device "
        "registration, manual refresh, or after a write), not live from "
        "the equipment. Requires observer role or higher; site-scoped "
        "users may only query devices in their allowed sites."
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
            device_version=config.device_version if config else None,
            hostname=config.hostname if config else None,
            snmp_enabled=config.snmp_enabled if config else None,
            snmp_version=config.snmp_version if config else None,
            snmp_community=config.snmp_community if config else None,
            snmp_permission=config.snmp_permission if config else None,
            ntp_server=config.ntp_server if config else None,
            dns_server=config.dns_server if config else None,
            log_server=config.log_server if config else None,
            log_level=config.log_level if config else None,
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


@router.patch(
    "/snmp",
    status_code=202,
    summary="Configure SNMP",
    description=(
        "Configure SNMP version and/or community+permission (RF-GLOBAL-07). "
        "`community` and `permission` must be provided together. No-op on "
        "sub-fields that already match the device's current state. "
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


@router.patch(
    "/log-servers",
    status_code=202,
    summary="Configure NTP/DNS/Log servers",
    description=(
        "Configure NTP server, DNS server, syslog server and/or log level "
        "(RF-GLOBAL-09 — the SRS groups these into a single use case). At "
        "least one sub-field must be provided; no-op on sub-fields that "
        "already match the device's current state. Executed asynchronously, "
        "same job-polling shape as the other global-config writes. Requires "
        "admin role or higher."
    ),
)
def set_global_config_log_servers(
    name: str,
    data: GlobalConfigLogServersUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = GlobalConfig(log_servers_update=data.model_dump(exclude_none=True))
    try:
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="admin", device=dev)
    _require_driver_with(dev, "set_log_servers")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})
