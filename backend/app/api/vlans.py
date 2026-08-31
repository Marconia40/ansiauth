import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.exceptions import DeviceExecutionError, NotFoundError, ValidationError
from app.core.response import ok
from app.core.scope import authorize_device, obtener_scope, require_authenticated
from app.models.visibility_scope import VisibilityScope
from app.models.vlan import VLAN
from app.schemas.vlan import VLANCreate, VLANDelete, VLANUpdate

logger = logging.getLogger(__name__)
router = APIRouter()


def _authz_devices(scope: VisibilityScope, device_names, *, min_role: str) -> None:
    """Enforce read/write access to every device in *device_names* against
    the caller's VisibilityScope. A VLAN request can target N devices, so
    require_scope()'s single-target pre-handler resolution doesn't fit --
    this loops and calls the shared authorize_device() per device (same
    helper api/jobs.py/api/ports.py use for their single-device case)."""
    for name in device_names:
        authorize_device(scope, name, "vlan_device_op", min_role)


def _leer_vlans_en_vivo(device_name: str) -> list[dict]:
    """FINAL_ARCHITECTURE.md §4 — GET /vlans lee en vivo, no de
    Repository[VLAN]. RedisCoordinator en vez de device_locks.acquire()
    directo (FASE_1.md, caller roto documentado)."""
    from app.composition import device_repository, redis_coordinator

    device = device_repository.get(device_name)
    if device is None:
        raise NotFoundError(f"Device '{device_name}' not found")
    try:
        with redis_coordinator.bloquear(device_name, timeout=10):
            vlans = device.driver.get_vlans(device, device.password)
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail={"status": "device_busy", "device": device_name, "message": "Device is busy with another operation, retry shortly"},
        )
    except RuntimeError as e:
        raise DeviceExecutionError(str(e))
    return [v.to_dict() for v in vlans]


@router.get(
    "/",
    summary="List VLANs",
    description=(
        "Retrieve the VLAN table from one or more devices. "
        "Pass `device=switch-01` for a single device or `devices=switch-01&devices=switch-02` "
        "for multiple. Requires observer role or higher."
    ),
)
def get_vlans(
    device: str | None = None,
    devices: list[str] | None = Query(default=None),
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    if devices:
        _authz_devices(scope, devices, min_role="observer")
        result = {dev: _leer_vlans_en_vivo(dev) for dev in devices}
        return ok(result)

    if device is None:
        from app.core.config import EXECUTION_MODE
        if EXECUTION_MODE != "mock":
            raise ValidationError("'device' query parameter is required")
        # Atajo de API para "no especifiqué device" en modo mock -- no es una
        # decisión de driver, FINAL_ARCHITECTURE.md ya lo marcó fuera de
        # alcance de esta migración. Fuente real sin pasar por vlan_service.py
        # (muerto tras esta fase, ver "Callers rotos" de FASE_5.md).
        from app.services.vendors.mock import _mock_vlans
        return ok([v.to_dict() for v in _mock_vlans])

    _authz_devices(scope, [device], min_role="observer")
    return ok(_leer_vlans_en_vivo(device))


@router.post(
    "/",
    summary="Create VLAN",
    description=(
        "Create a new VLAN on one or more devices via Ansible. "
        "Each device gets its own background job — the response contains a job ID per device. "
        "Poll `GET /api/v1/jobs/{job_id}` for execution status. "
        "Requires operator role or higher on every target device."
    ),
)
def create_vlan(
    vlan: VLANCreate,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_repository, group_operation_runner

    entidad = VLAN(vlan_id=vlan.vlan_id, name=vlan.name)  # __post_init__ valida vlan_id
    try:
        entidad.validar()  # __post_init__ NO valida name (Fase 2) -- corrección
        # real: el snippet canónico de esta fase no lo llamaba, un nombre
        # inválido pasaría con 200 y recién fallaría adentro del job en vez
        # del 400 inmediato que ya da el código actual.
    except ValueError as e:
        raise ValidationError(str(e))
    for dev_name in vlan.devices:
        if device_repository.get(dev_name) is None:
            raise NotFoundError(f"Device '{dev_name}' not found")
    # require_authenticated/obtener_scope arriba autentican -- acá se chequea
    # el rol real por cada device destino, ninguno se salta la autorización.
    _authz_devices(scope, vlan.devices, min_role="operator")
    group_job_id, jobs = group_operation_runner.encolar(entidad, vlan.devices, current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.delete(
    "/{vlan_id}",
    summary="Delete VLAN",
    description=(
        "Remove a VLAN from one or more devices via Ansible. "
        "Returns a job ID per device. Reserved VLANs (1, 1002–1005) cannot be deleted. "
        "Requires admin role on every target device."
    ),
)
def delete_vlan(
    vlan_id: int,
    data: VLANDelete,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_repository, group_operation_runner

    entidad = VLAN(vlan_id=vlan_id, eliminar=True)  # __post_init__ valida vlan_id
    for dev_name in data.devices:
        if device_repository.get(dev_name) is None:
            raise NotFoundError(f"Device '{dev_name}' not found")
    # Legacy delete_vlan required admin; ESC-3 keeps that gate — DELETE is
    # coarser-grained than create/update (harder to reverse) so it stays
    # admin-only per device.
    _authz_devices(scope, data.devices, min_role="admin")
    group_job_id, jobs = group_operation_runner.encolar(entidad, data.devices, current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)


@router.patch(
    "/{vlan_id}",
    summary="Update VLAN",
    description=(
        "Update the description of an existing VLAN on one or more devices. "
        "Returns a job ID per device. Requires operator role or higher on every target device."
    ),
)
def update_vlan(
    vlan_id: int,
    data: VLANUpdate,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_repository, group_operation_runner

    try:
        entidad = VLAN(vlan_id=vlan_id, name=data.description)  # __post_init__ valida vlan_id
        entidad.validar()  # name -- __post_init__ no lo valida (Fase 2), ver nota en create_vlan()
    except ValueError as e:
        raise ValidationError(str(e))
    for dev_name in data.devices:
        if device_repository.get(dev_name) is None:
            raise NotFoundError(f"Device '{dev_name}' not found")
    _authz_devices(scope, data.devices, min_role="operator")
    group_job_id, jobs = group_operation_runner.encolar(entidad, data.devices, current_user["username"])
    return ok(group_job_id=group_job_id, jobs=jobs)
