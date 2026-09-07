from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.exceptions import ValidationError
from app.core.response import ok
from app.core.scope import authorize_device, obtener_scope, require_authenticated, require_device
from app.models.port import Puerto
from app.models.visibility_scope import VisibilityScope
from app.schemas.device_sync import SyncedResource
from app.schemas.port import (
    PortAccessVlanUpdateRequest,
    PortAdminStateUpdateRequest,
    PortBatchChangeItem,
    PortBatchRequest,
    PortDescriptionClearRequest,
    PortDescriptionUpdateRequest,
    PortEnableRequest,
    PortPoeUpdateRequest,
    PortRead,
    PortResetRequest,
    PortSetAccessModeRequest,
    PortSetTrunkModeRequest,
    PortShutdownRequest,
    PortStormControlUpdateRequest,
    PortTrunkVlansUpdateRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()

def _authz_device(
    scope: VisibilityScope, device_name: str, *, min_role: str, device: "Device | None" = None,
) -> None:
    """Enforce read/write access to *device_name* against the caller's
    VisibilityScope. 10 endpoints here each need a different min_role, so
    a single require_scope() op wouldn't cover the family -- this stays an
    explicit call, but delegates to the one shared authorize_device()
    (api/jobs.py/api/vlans.py use the same helper, no more 3 duplicate
    ranking dicts).

    *device*, when the caller already fetched it via require_device(),
    lets authorize_device() skip its own JOIN query -- (site_id,
    device_group_id) are already populated on the domain object.
    Duplication found in a code review: every write endpoint here already
    has the Device in hand by the time this runs."""
    resolved = (device.site_id, device.device_group_id) if device is not None else None
    authorize_device(scope, device_name, "port_device_op", min_role, resolved=resolved)


def _require_port_driver_with(device: "Device", method_name: str):
    """Verifica que el driver del *device* sobreescriba *method_name* antes
    de encolar el job -- corrección de Fase 1/A2 aplicada acá (FASE_5.md
    A7): la real comparaba contra ``BasePortDriver``, que ya no existe
    desde que Fase 1 lo fusionó en ``VendorDriver``. Devuelve 501 con el
    mismo envelope ``VENDOR_NOT_SUPPORTED`` que la real."""
    from app.services.vendors.base import VendorDriver

    driver = device.driver
    if getattr(type(driver), method_name) is getattr(VendorDriver, method_name):
        logger.info(
            "Port %s unimplemented on driver=%s for device=%s",
            method_name, type(driver).__name__, device.name,
        )
        raise HTTPException(
            status_code=501,
            detail={
                "error_code": "VENDOR_NOT_SUPPORTED",
                "message": "This port operation is not yet supported for this vendor.",
            },
        )
    return driver


@router.get(
    "/",
    summary="List ports",
    description=(
        "Retrieve the normalized port inventory of a single device. "
        "Returns interface name, description, admin and operational "
        "state, switchport mode, access / trunk VLAN information, and "
        "(when exposed by the device) PoE / speed / duplex.  Read-only — "
        "no configuration changes.  Requires observer role or higher; "
        "site-scoped users may only query devices in their allowed sites."
    ),
)
def list_ports(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    """Return the port inventory of *name* -- cache-first.

    Lee de ``Repository[Puerto]`` (populated por ``sync_device_task``,
    disparado en el alta / por refresh manual / post-escritura), no en
    vivo del equipo. Responde instantáneo aún si el equipo está apagado;
    para forzar una sync fresca ver ``POST /devices/{name}/ports/refresh``.

    La respuesta viaja envuelta en ``SyncedResource`` con
    ``synced_at`` / ``sync_error`` / ``sync_in_progress`` para que la
    UI pueda mostrar "última sync hace X min" y decidir cuándo refrescar.
    """
    from app.composition import device_sync_service, puerto_repository, redis_coordinator

    _authz_device(scope, name, min_role="observer")
    dev = require_device(name)
    puertos = puerto_repository.list(device=name)
    payload = {
        "device": dev.name,
        "vendor": dev.vendor,
        "count": len(puertos),
        "ports": [
            PortRead(
                name=p.interface, description=p.description, admin_up=p.admin_up,
                operational_up=p.operational_up, mode=p.mode or "unknown", access_vlan=p.access_vlan,
                allowed_vlans=p.allowed_vlans,
                storm_control_enabled=p.storm_control_enabled,
                storm_control_threshold=p.storm_control_threshold,
            ).model_dump()
            for p in puertos
        ],
    }
    synced_at, sync_error = device_sync_service.metadata(name, "ports")
    envelope = SyncedResource(
        data=payload,
        synced_at=synced_at,
        sync_error=sync_error,
        sync_in_progress=redis_coordinator.esta_ocupado(name),
    ).model_dump(mode="json")
    return ok(envelope)


@router.patch(
    "/description",
    status_code=202,
    summary="Set port description",
    description=(
        "Assign a description to a single interface on a device.  To "
        "clear it, use `DELETE .../description` instead — a value is "
        "always required here.  Executed asynchronously: the response "
        "carries a `group_job_id` and per-device job entry the frontend "
        "can poll via `GET /api/v1/jobs/{job_id}` and "
        "`GET /api/v1/group-jobs/{id}`.  Pre-state is captured for "
        "rollback — if the device-side change fails, the original "
        "description is restored automatically.  Requires operator role "
        "or higher; site-scoped users may only target devices in their "
        "allowed sites."
    ),
)
def update_port_description(
    name: str,
    data: PortDescriptionUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, description=data.description)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "update_port_description")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.delete(
    "/description",
    status_code=202,
    summary="Clear port description",
    description=(
        "Clear the description of a single interface on a device "
        "(`undo description` on Huawei VRP, `no description` on Cisco "
        "IOS).  Executed asynchronously: the response carries a "
        "`group_job_id` and per-device job entry the frontend can poll "
        "via `GET /api/v1/jobs/{job_id}` and `GET /api/v1/group-jobs/{id}`.  "
        "Pre-state is captured for rollback — if the device-side change "
        "fails, the original description is restored automatically.  "
        "Requires operator role or higher; site-scoped users may only "
        "target devices in their allowed sites."
    ),
)
def clear_port_description(
    name: str,
    data: PortDescriptionClearRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = Puerto(interface=data.interface, description="")
    entidad.validar()

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "update_port_description")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.patch(
    "/admin-state",
    status_code=202,
    summary="Set port admin state",
    description=(
        "Administratively enable or disable a single interface.  "
        "``enabled=true`` runs ``undo shutdown`` (Huawei) / ``no shutdown`` "
        "(Cisco); ``enabled=false`` runs ``shutdown``.  Executed "
        "asynchronously: the response carries a ``group_job_id`` and per-"
        "device job entry the frontend can poll via "
        "``GET /api/v1/jobs/{job_id}`` and ``GET /api/v1/group-jobs/{id}``.  "
        "Pre-state is captured for rollback — if the device-side change "
        "fails, the original admin state is restored automatically.  "
        "Requires operator role or higher; site-scoped users may only "
        "target devices in their allowed sites."
    ),
)
def set_port_admin_state(
    name: str,
    data: PortAdminStateUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, admin_up=data.enabled)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "set_port_admin_state")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.patch(
    "/access-vlan",
    status_code=202,
    summary="Set port access VLAN",
    description=(
        "Assign an access VLAN to a single interface — or the native VLAN "
        "(PVID) if the port is currently in trunk mode.  The orchestration "
        "layer reads the port's live mode and dispatches accordingly; "
        "other modes are rejected.  Executed asynchronously: the response "
        "carries a ``group_job_id`` and per-device job entry the frontend "
        "can poll via ``GET /api/v1/jobs/{job_id}`` and "
        "``GET /api/v1/group-jobs/{id}``.  Pre-state is captured for "
        "rollback — if the device-side change fails, the original VLAN is "
        "restored automatically.  Requires operator role or higher; "
        "site-scoped users may only target devices in their allowed sites."
    ),
)
def set_port_access_vlan(
    name: str,
    data: PortAccessVlanUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    """No setea ``mode`` en el ``Puerto`` a propósito -- ``Puerto.aplicar()``
    lee el modo en vivo del device (FASE_5.md A7, corrección
    ``_aplicar_access_vlan``/``Puerto.validar()``)."""
    from app.composition import group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, access_vlan=data.vlan_id)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "set_port_access_vlan")
    # Puerto._aplicar_access_vlan() despacha a set_trunk_pvid_vlan() en vez
    # de acá cuando el puerto resulta estar en modo trunk (access_vlan es
    # el PVID en ese caso) -- bug real encontrado en una revisión de
    # código: este gate solo chequeaba el método que NO se termina
    # llamando en ese escenario. Un driver que implemente uno sin el otro
    # pasaba el gate (200, job encolado) y explotaba después con
    # NotImplementedError crudo adentro del worker en vez del 501 limpio
    # que este chequeo existe para dar.
    _require_port_driver_with(dev, "set_trunk_pvid_vlan")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.patch(
    "/trunk-vlans",
    status_code=202,
    summary="Set trunk allowed VLANs",
    description=(
        "Modify the trunk allowed-VLAN list on a single interface.  "
        "``mode='replace'`` sets the list to exactly ``vlans``; "
        "``mode='add'`` unions ``vlans`` with the current list; "
        "``mode='remove'`` subtracts ``vlans`` from the current list.  "
        "The port must already be in trunk mode; a ``remove`` that would "
        "leave the trunk with no allowed VLANs is rejected.  Executed "
        "asynchronously: the response carries a ``group_job_id`` and per-"
        "device job entry the frontend can poll via "
        "``GET /api/v1/jobs/{job_id}`` and ``GET /api/v1/group-jobs/{id}``.  "
        "Pre-state is captured for rollback — if the device-side change "
        "fails, the original VLAN list is restored automatically.  "
        "Requires operator role or higher; site-scoped users may only "
        "target devices in their allowed sites."
    ),
)
def set_trunk_allowed_vlans(
    name: str,
    data: PortTrunkVlansUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    """``data.mode`` ("replace"/"add"/"remove") es
    ``Puerto.allowed_vlan_operation``, no ``Puerto.mode`` (switchport
    mode) -- mismo nombre, dos conceptos distintos (FASE_5.md A7)."""
    from app.composition import group_operation_runner

    try:
        entidad = Puerto(
            interface=data.interface,
            allowed_vlans=list(data.vlans),
            allowed_vlan_operation=data.mode,
        )
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "set_trunk_allowed_vlans")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.post(
    "/access-mode",
    status_code=202,
    summary="Set port to access mode",
    description=(
        "Set a single interface to access mode with the given access VLAN, "
        "atomically (mode + VLAN applied together).  "
        "Replaces the old generic ``/configure`` endpoint for this specific, "
        "well-defined operation — there is no meaningful 'just change mode, "
        "keep whatever VLAN was there' case.  "
        "Pre-state is captured for rollback — if the device-side change "
        "fails, the port's original mode/VLAN are restored automatically.  "
        "Executed asynchronously: the response carries a ``group_job_id`` and "
        "per-device job entry the frontend can poll via ``GET /api/v1/jobs/{job_id}`` "
        "and ``GET /api/v1/group-jobs/{id}``.  "
        "Requires operator role or higher; site-scoped users may only target "
        "devices in their allowed sites."
    ),
)
def set_port_access_mode(
    name: str,
    data: PortSetAccessModeRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, mode="access", access_vlan=data.access_vlan)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "set_access_mode")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.post(
    "/trunk-mode",
    status_code=202,
    summary="Set port to trunk mode",
    description=(
        "Set a single interface to trunk mode with the given native VLAN "
        "(PVID) and allowed-VLAN list, atomically (mode + both VLAN "
        "dimensions applied together).  Both always fully replace whatever "
        "the port had before — this is a mode change, not an add/remove "
        "relative to an existing trunk (the port may be coming from access "
        "mode with no prior trunk config at all).  Use "
        "``PATCH /ports/access-vlan``/``PATCH /ports/trunk-vlans`` to adjust "
        "either dimension individually on a port that's already trunk.  "
        "Pre-state is captured for rollback — if the device-side change "
        "fails, the port's original mode/VLANs are restored automatically.  "
        "Executed asynchronously: the response carries a ``group_job_id`` and "
        "per-device job entry the frontend can poll via ``GET /api/v1/jobs/{job_id}`` "
        "and ``GET /api/v1/group-jobs/{id}``.  "
        "Requires operator role or higher; site-scoped users may only target "
        "devices in their allowed sites."
    ),
)
def set_port_trunk_mode(
    name: str,
    data: PortSetTrunkModeRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    try:
        entidad = Puerto(
            interface=data.interface, mode="trunk",
            access_vlan=data.native_vlan, allowed_vlans=list(data.allowed_vlans),
        )
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "set_trunk_mode")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.post(
    "/shutdown",
    status_code=202,
    summary="Shutdown port",
    description=(
        "Administratively disable a single interface (``shutdown`` command).  "
        "No-op when the port is already administratively down.  "
        "Pre-state is captured for rollback — if the device-side command fails "
        "and the port was previously up, the original admin state is restored "
        "automatically.  "
        "Executed asynchronously: the response carries a ``group_job_id`` and "
        "per-device job entry.  "
        "Requires operator role or higher; site-scoped users may only target "
        "devices in their allowed sites."
    ),
)
def shutdown_port(
    name: str,
    data: PortShutdownRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    """Wrapper semántico de ``admin_up=False`` -- no existe
    ``driver.shutdown_port()`` en el camino nuevo (``VendorDriver`` lo
    declara pero ningún caller real lo invoca, confirmado: ``Puerto``
    siempre despacha a ``set_port_admin_state``). El gate de driver
    soportado chequea ``"set_port_admin_state"``, no ``"shutdown_port"``
    -- corrección real encontrada acá: chequear el nombre del endpoint
    viejo hubiera devuelto 501 siempre, ya que ningún driver sobreescribe
    ese stub (FASE_5.md A7)."""
    from app.composition import group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, admin_up=False)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "set_port_admin_state")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.post(
    "/enable",
    status_code=202,
    summary="Enable port",
    description=(
        "Administratively enable a single interface (``no shutdown`` / "
        "``undo shutdown`` command).  "
        "No-op when the port is already administratively up.  "
        "Pre-state is captured for rollback — if the device-side command fails "
        "and the port was previously down, the original admin state is restored "
        "automatically.  "
        "Executed asynchronously: the response carries a ``group_job_id`` and "
        "per-device job entry.  "
        "Requires operator role or higher; site-scoped users may only target "
        "devices in their allowed sites."
    ),
)
def enable_port(
    name: str,
    data: PortEnableRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    """Wrapper semántico de ``admin_up=True`` -- ver nota en
    ``shutdown_port()``."""
    from app.composition import group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, admin_up=True)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "set_port_admin_state")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.patch(
    "/poe",
    status_code=202,
    summary="Set port PoE state",
    description=(
        "Enable or disable Power-over-Ethernet on a single interface "
        "(RF-PUERTO-09).  ``enabled=true`` runs ``power inline auto`` "
        "(Cisco) / ``poe enable`` (Huawei); ``enabled=false`` runs "
        "``power inline never`` / ``poe disable``.  Executed asynchronously: "
        "the response carries a ``group_job_id`` and per-device job entry "
        "the frontend can poll via ``GET /api/v1/jobs/{job_id}`` and "
        "``GET /api/v1/group-jobs/{id}``.  Requires operator role or "
        "higher; site-scoped users may only target devices in their "
        "allowed sites."
    ),
)
def set_port_poe(
    name: str,
    data: PortPoeUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, poe_enabled=data.enabled)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "set_port_poe")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.patch(
    "/storm-control",
    status_code=202,
    summary="Set port storm-control state",
    description=(
        "Enable or disable broadcast storm-control on a single interface, "
        "with a single percentage threshold (RF-PUERTO-07, simplified "
        "scope — one enable flag + one global threshold, not the 3 traffic "
        "types real hardware exposes separately).  ``threshold_percent`` is "
        "required when ``enabled=true``.  Executed asynchronously: the "
        "response carries a ``group_job_id`` and per-device job entry.  "
        "Requires operator role or higher; site-scoped users may only "
        "target devices in their allowed sites."
    ),
)
def set_port_storm_control(
    name: str,
    data: PortStormControlUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    try:
        entidad = Puerto(
            interface=data.interface,
            storm_control_enabled=data.enabled,
            storm_control_threshold=data.threshold_percent,
        )
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "set_storm_control")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.post(
    "/reset",
    status_code=202,
    summary="Reset port to defaults",
    description=(
        "Reset a single interface to its factory-default configuration "
        "(RF-PUERTO-10 — ``default interface`` on Cisco, ``clear "
        "configuration interface`` on Huawei).  Clears VLAN assignment, "
        "description, PoE, storm-control and admin state back to device "
        "defaults.  Executed asynchronously: the response carries a "
        "``group_job_id`` and per-device job entry.  Requires operator "
        "role or higher; site-scoped users may only target devices in "
        "their allowed sites."
    ),
)
def reset_port(
    name: str,
    data: PortResetRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    entidad = Puerto(interface=data.interface, reset=True)
    entidad.validar()

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)
    _require_port_driver_with(dev, "reset_port")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [name], current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


def expandir_a_puertos(cambio: PortBatchChangeItem) -> list[Puerto]:
    """1 entrada de ``PortBatchRequest.changes`` -> 1+ ``Puerto``, agrupando
    los mismos combos que ``Puerto.aplicar()`` ya conoce (``mode``+
    ``access_vlan``(+``allowed_vlans``) en 1 solo ``Puerto``, storm-control
    en otro) y 1 ``Puerto`` por campo suelto para el resto -- cada
    ``Puerto`` resultante pasa por ``Puerto.validar()`` sin cambios, la
    única pieza nueva es este agrupamiento."""
    puertos: list[Puerto] = []
    if cambio.mode is not None:
        puertos.append(Puerto(
            interface=cambio.interface, mode=cambio.mode, access_vlan=cambio.access_vlan,
            allowed_vlans=list(cambio.allowed_vlans) if cambio.allowed_vlans else None,
        ))
    else:
        if cambio.access_vlan is not None:
            puertos.append(Puerto(interface=cambio.interface, access_vlan=cambio.access_vlan))
        if cambio.allowed_vlans is not None:
            puertos.append(Puerto(
                interface=cambio.interface, allowed_vlans=list(cambio.allowed_vlans),
                allowed_vlan_operation=cambio.allowed_vlan_operation,
            ))
    if cambio.storm_control_enabled is not None:
        puertos.append(Puerto(
            interface=cambio.interface, storm_control_enabled=cambio.storm_control_enabled,
            storm_control_threshold=cambio.storm_control_threshold,
        ))
    if cambio.description is not None:
        puertos.append(Puerto(interface=cambio.interface, description=cambio.description))
    if cambio.admin_up is not None:
        puertos.append(Puerto(interface=cambio.interface, admin_up=cambio.admin_up))
    if cambio.poe_enabled is not None:
        puertos.append(Puerto(interface=cambio.interface, poe_enabled=cambio.poe_enabled))
    if not puertos:
        raise ValueError(f"no changes provided for interface {cambio.interface!r}")
    return puertos


@router.post(
    "/batch",
    status_code=202,
    summary="Batch-update multiple ports (or multiple fields on one port) in 1 connection",
    description=(
        "Apply N changes -- across multiple interfaces, multiple fields on "
        "one interface, or any mix -- in a **single** SSH connection to the "
        "device instead of one connection per change. Each entry in "
        "`changes` may carry the full config of one port (multiple fields "
        "together) or just one field. Field-level no-op detection still "
        "applies per entry (unchanged values aren't re-sent). If the batch "
        "fails partway, the whole job fails and rollback attempts to "
        "restore every entry that did change -- there is no partial-success "
        "reporting per entry. Executed asynchronously: the response "
        "carries a `group_job_id` and a single job entry (1 job = 1 "
        "connection, not 1 per change). Requires operator role or higher; "
        "site-scoped users may only target devices in their allowed sites."
    ),
)
def batch_update_ports(
    name: str,
    data: PortBatchRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    recursos: list[Puerto] = []
    try:
        for cambio in data.changes:
            nuevos = expandir_a_puertos(cambio)
            for puerto in nuevos:
                puerto.validar()
            recursos.extend(nuevos)
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = require_device(name)
    _authz_device(scope, name, min_role="operator", device=dev)

    group_job_id, job_entry = group_operation_runner.encolar_lote(recursos, name, current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": [job_entry]})
