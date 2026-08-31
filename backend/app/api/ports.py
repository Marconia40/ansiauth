from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.exceptions import DeviceExecutionError, NotFoundError, ValidationError
from app.core.response import ok
from app.core.scope import authorize_device, obtener_scope, require_authenticated
from app.models.port import Puerto
from app.models.visibility_scope import VisibilityScope
from app.schemas.port import (
    PortAccessVlanUpdateRequest,
    PortAdminStateUpdateRequest,
    PortDescriptionUpdateRequest,
    PortEnableRequest,
    PortRead,
    PortSetAccessModeRequest,
    PortSetTrunkModeRequest,
    PortShutdownRequest,
    PortTrunkVlansUpdateRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter()

def _authz_device(scope: VisibilityScope, device_name: str, *, min_role: str) -> None:
    """Enforce read/write access to *device_name* against the caller's
    VisibilityScope. 10 endpoints here each need a different min_role, so
    a single require_scope() op wouldn't cover the family -- this stays an
    explicit call, but delegates to the one shared authorize_device()
    (api/jobs.py/api/vlans.py use the same helper, no more 3 duplicate
    ranking dicts)."""
    authorize_device(scope, device_name, "port_device_op", min_role)


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


def _check_device_not_locked(device_name: str) -> None:
    """Raise 409 immediately when the device is already held by another
    operation. Non-blocking probe via RedisCoordinator -- reemplaza
    device_locks.is_device_busy() (FASE_1.md, RedisCoordinator fusiona
    device_locks.py + rate_limiter.py). Misma ventana TOCTOU que la real:
    el chequeo es best-effort, Orquestador adquiere su propio lock al
    ejecutar el job."""
    from app.composition import redis_coordinator

    if redis_coordinator.esta_ocupado(device_name):
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "DEVICE_LOCKED",
                "message": (
                    f"Device '{device_name}' is busy with another operation — "
                    "retry shortly"
                ),
            },
        )


@router.get(
    "/",
    summary="List ports",
    description=(
        "Retrieve the normalized port inventory of a single device. "
        "Pass `device=<name>` as a query parameter.  Returns interface "
        "name, description, admin and operational state, switchport mode, "
        "access / trunk VLAN information, and (when exposed by the device) "
        "PoE / speed / duplex.  Read-only — no configuration changes. "
        "Requires observer role or higher; site-scoped users may only "
        "query devices in their allowed sites."
    ),
)
def list_ports(
    device: str | None = None,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    """Return the port inventory of *device*.

    Read-only. Lee en vivo (``device.driver.list_ports``), no de
    ``Repository[Puerto]`` — mismo criterio que ``GET /vlans``
    (FASE_5.md A6/A7). ``PortRead`` usa ``name``, ``Puerto`` usa
    ``interface`` — la traducción es explícita acá (FASE_5.md A7).
    """
    from app.composition import device_repository, redis_coordinator

    if device is None:
        from app.core.config import EXECUTION_MODE
        if EXECUTION_MODE != "mock":
            raise ValidationError("'device' query parameter is required")
        from app.services.vendors.mock import _INITIAL_MOCK_PORTS
        puertos = list(_INITIAL_MOCK_PORTS)
        payload = {
            "device": "mock_device",
            "vendor": "mock",
            "count": len(puertos),
            "ports": [
                PortRead(
                    name=p.interface, description=p.description, admin_up=p.admin_up,
                    operational_up=p.operational_up, mode=p.mode, access_vlan=p.access_vlan,
                    allowed_vlans=p.allowed_vlans, poe_enabled=p.poe_enabled,
                    speed=p.speed, duplex=p.duplex,
                ).model_dump()
                for p in puertos
            ],
        }
        return ok(payload)

    _authz_device(scope, device, min_role="observer")
    dev = device_repository.get(device)
    if dev is None:
        raise NotFoundError(f"Device '{device}' not found")
    try:
        with redis_coordinator.bloquear(device, timeout=10):
            puertos = dev.driver.list_ports(dev, dev.password)
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "device_busy",
                "device": device,
                "message": "Device is busy with another operation, retry shortly",
            },
        )
    except RuntimeError as exc:
        raise DeviceExecutionError(str(exc))

    payload = {
        "device": dev.name,
        "vendor": dev.vendor,
        "count": len(puertos),
        "ports": [
            PortRead(
                name=p.interface, description=p.description, admin_up=p.admin_up,
                operational_up=p.operational_up, mode=p.mode, access_vlan=p.access_vlan,
                allowed_vlans=p.allowed_vlans, poe_enabled=p.poe_enabled,
                speed=p.speed, duplex=p.duplex,
            ).model_dump()
            for p in puertos
        ],
    }
    return ok(payload)


@router.patch(
    "/description",
    summary="Update port description",
    description=(
        "Update the description of a single interface on a device.  An "
        "empty description clears the description (`undo description` on "
        "Huawei VRP, `no description` on Cisco IOS).  Executed "
        "asynchronously: the response carries a `group_job_id` and per-"
        "device job entry the frontend can poll via "
        "`GET /api/v1/jobs/{job_id}` and `GET /api/v1/group-jobs/{id}`.  "
        "Pre-state is captured for rollback — if the device-side change "
        "fails, the original description is restored automatically.  "
        "Requires operator role or higher; site-scoped users may only "
        "target devices in their allowed sites."
    ),
)
def update_port_description(
    data: PortDescriptionUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_repository, group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, description=data.description)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = device_repository.get(data.device)
    if dev is None:
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(scope, data.device, min_role="operator")
    _check_device_not_locked(data.device)
    _require_port_driver_with(dev, "update_port_description")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [data.device], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.patch(
    "/admin-state",
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
    data: PortAdminStateUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_repository, group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, admin_up=data.enabled)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = device_repository.get(data.device)
    if dev is None:
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(scope, data.device, min_role="operator")
    _check_device_not_locked(data.device)
    _require_port_driver_with(dev, "set_port_admin_state")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [data.device], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.patch(
    "/access-vlan",
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
    data: PortAccessVlanUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    """No setea ``mode`` en el ``Puerto`` a propósito -- ``Puerto.aplicar()``
    lee el modo en vivo del device (FASE_5.md A7, corrección
    ``_aplicar_access_vlan``/``Puerto.validar()``)."""
    from app.composition import device_repository, group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, access_vlan=data.vlan_id)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = device_repository.get(data.device)
    if dev is None:
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(scope, data.device, min_role="operator")
    _check_device_not_locked(data.device)
    _require_port_driver_with(dev, "set_port_access_vlan")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [data.device], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.patch(
    "/trunk-vlans",
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
    data: PortTrunkVlansUpdateRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    """``data.mode`` ("replace"/"add"/"remove") es
    ``Puerto.allowed_vlan_operation``, no ``Puerto.mode`` (switchport
    mode) -- mismo nombre, dos conceptos distintos (FASE_5.md A7)."""
    from app.composition import device_repository, group_operation_runner

    try:
        entidad = Puerto(
            interface=data.interface,
            allowed_vlans=list(data.vlans),
            allowed_vlan_operation=data.mode,
        )
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = device_repository.get(data.device)
    if dev is None:
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(scope, data.device, min_role="operator")
    _check_device_not_locked(data.device)
    _require_port_driver_with(dev, "set_trunk_allowed_vlans")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [data.device], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.post(
    "/access-mode",
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
    data: PortSetAccessModeRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_repository, group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, mode="access", access_vlan=data.access_vlan)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = device_repository.get(data.device)
    if dev is None:
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(scope, data.device, min_role="operator")
    _check_device_not_locked(data.device)
    _require_port_driver_with(dev, "set_access_mode")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [data.device], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.post(
    "/trunk-mode",
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
    data: PortSetTrunkModeRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_repository, group_operation_runner

    try:
        entidad = Puerto(
            interface=data.interface, mode="trunk",
            access_vlan=data.native_vlan, allowed_vlans=list(data.allowed_vlans),
        )
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = device_repository.get(data.device)
    if dev is None:
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(scope, data.device, min_role="operator")
    _check_device_not_locked(data.device)
    _require_port_driver_with(dev, "set_trunk_mode")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [data.device], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.post(
    "/shutdown",
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
    from app.composition import device_repository, group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, admin_up=False)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = device_repository.get(data.device)
    if dev is None:
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(scope, data.device, min_role="operator")
    _check_device_not_locked(data.device)
    _require_port_driver_with(dev, "set_port_admin_state")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [data.device], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.post(
    "/enable",
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
    data: PortEnableRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    """Wrapper semántico de ``admin_up=True`` -- ver nota en
    ``shutdown_port()``."""
    from app.composition import device_repository, group_operation_runner

    try:
        entidad = Puerto(interface=data.interface, admin_up=True)
        entidad.validar()
    except ValueError as exc:
        raise ValidationError(str(exc))

    dev = device_repository.get(data.device)
    if dev is None:
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(scope, data.device, min_role="operator")
    _check_device_not_locked(data.device)
    _require_port_driver_with(dev, "set_port_admin_state")

    group_job_id, jobs = group_operation_runner.encolar(entidad, [data.device], current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)
