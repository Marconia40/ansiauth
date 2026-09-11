import logging

from fastapi import APIRouter, Depends, Query

from app.core.exceptions import ValidationError
from app.core.response import ok
from app.core.scope import authorize_device, obtener_scope, require_authenticated, require_device
from app.models.visibility_scope import VisibilityScope
from app.models.vlan import VLAN
from app.schemas.device_sync import SyncedResource
from app.schemas.vlan import VLANBatchRequest, VLANCreate, VLANDelete, VLANUpdate

logger = logging.getLogger(__name__)
router = APIRouter()


def _authz_devices(scope: VisibilityScope, device_names, *, min_role: str, resolved_by_name=None) -> None:
    """Enforce read/write access to every device in *device_names* against
    the caller's VisibilityScope. A VLAN request can target N devices, so
    require_scope()'s single-target pre-handler resolution doesn't fit --
    this loops and calls the shared authorize_device() per device (same
    helper api/jobs.py/api/ports.py use for their single-device case).

    *resolved_by_name*, when given (``{name: Device}``, from callers that
    already ran require_device() on every name), lets each
    authorize_device() call skip its own JOIN query -- duplication found
    in a code review: create_vlan()/delete_vlan()/update_vlan() already
    fetch every device once (for the existence check) before this runs."""
    for name in device_names:
        dev = resolved_by_name.get(name) if resolved_by_name else None
        resolved = (dev.site_id, dev.device_group_id) if dev is not None else None
        authorize_device(scope, name, "vlan_device_op", min_role, resolved=resolved)


def _leer_vlans_cache(device_name: str) -> dict:
    """Cache-first read del estado de VLANs -- reemplaza el GET live
    (``_leer_vlans_en_vivo``) que hacía SSH cada vez.

    El SSH al equipo ya no vive acá: lo dispara ``sync_device_task``
    (Celery) desde el alta (``Inventory.register``), el endpoint de
    refresh (``POST /devices/{name}/vlans/refresh``) o después de una
    escritura exitosa. Este endpoint sólo lee ``device_vlans`` +
    metadata de freshness del ``DeviceModel``, así que responde
    instantáneo aún si el equipo está apagado.

    Devuelve el envelope ``SyncedResource`` para que la UI pueda
    mostrar "última sync hace X min" y decidir cuándo pedir refresh.
    """
    from app.composition import device_sync_service, redis_coordinator, vlan_repository

    require_device(device_name)  # 404 si no existe / raise
    vlans = vlan_repository.list(device=device_name)
    synced_at, sync_error = device_sync_service.metadata(device_name, "vlans")
    return SyncedResource(
        data=[v.to_dict() for v in vlans],
        synced_at=synced_at,
        sync_error=sync_error,
        sync_in_progress=redis_coordinator.esta_ocupado(device_name),
    ).model_dump(mode="json")


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
        # Multi-device: un envelope por device -- el shape del value
        # cambió de ``list[dict]`` (live) a ``SyncedResource`` (cache),
        # así el frontend obtiene freshness metadata por cada device.
        result = {dev: _leer_vlans_cache(dev) for dev in devices}
        return ok(result)

    if device is None:
        from app.core.config import EXECUTION_MODE
        if EXECUTION_MODE != "mock":
            raise ValidationError("'device' query parameter is required")
        # Atajo de API para "no especifiqué device" en modo mock -- data
        # sintética envuelta en el mismo SyncedResource para no romper el
        # shape del endpoint. synced_at=None marca "sin sync real".
        from app.services.vendors.mock import _mock_vlans
        envelope = SyncedResource(
            data=[v.to_dict() for v in _mock_vlans],
            synced_at=None, sync_error=None, sync_in_progress=False,
        ).model_dump(mode="json")
        return ok(envelope)

    _authz_devices(scope, [device], min_role="observer")
    return ok(_leer_vlans_cache(device))


@router.post(
    "/",
    status_code=202,
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
    from app.composition import group_operation_runner

    try:
        # VLAN(...) tiene que ir DENTRO del try -- bug real encontrado en
        # una revisión de código: __post_init__ ya valida vlan_id (rango/
        # reservado) y antes se construía la entidad afuera del try,
        # así que un vlan_id reservado (1, 1002-1005) o fuera de rango
        # tiraba un ValueError sin capturar -> 500 en vez del 400 que
        # ValidationError da. update_vlan(), más abajo, ya lo hacía bien.
        entidad = VLAN(vlan_id=vlan.vlan_id, name=vlan.name)
        entidad.validar()  # __post_init__ NO valida name (Fase 2) -- corrección
        # real: el snippet canónico de esta fase no lo llamaba, un nombre
        # inválido pasaría con 200 y recién fallaría adentro del job en vez
        # del 400 inmediato que ya da el código actual.
    except ValueError as e:
        raise ValidationError(str(e))
    devices_by_name = {name: require_device(name) for name in vlan.devices}
    # require_authenticated/obtener_scope arriba autentican -- acá se chequea
    # el rol real por cada device destino, ninguno se salta la autorización.
    # resolved_by_name reusa los Device ya buscados arriba -- evita repetir
    # el mismo JOIN 2 veces por device (duplicación encontrada en una
    # revisión de código).
    _authz_devices(scope, vlan.devices, min_role="operator", resolved_by_name=devices_by_name)
    group_job_id, jobs = group_operation_runner.encolar(entidad, vlan.devices, current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.delete(
    "/{vlan_id}",
    status_code=202,
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
    from app.composition import group_operation_runner

    try:
        # Mismo bug que create_vlan() -- ver esa nota. VLAN(...) sin try
        # acá dejaba un vlan_id reservado/fuera de rango tirar 500, a
        # pesar de que la propia descripción del endpoint dice "Reserved
        # VLANs (1, 1002-1005) cannot be deleted" como si diera un 400.
        entidad = VLAN(vlan_id=vlan_id, eliminar=True)
    except ValueError as e:
        raise ValidationError(str(e))
    devices_by_name = {name: require_device(name) for name in data.devices}
    # Legacy delete_vlan required admin; ESC-3 keeps that gate — DELETE is
    # coarser-grained than create/update (harder to reverse) so it stays
    # admin-only per device.
    _authz_devices(scope, data.devices, min_role="admin", resolved_by_name=devices_by_name)
    group_job_id, jobs = group_operation_runner.encolar(entidad, data.devices, current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})


@router.post(
    "/batch",
    status_code=202,
    summary="Batch VLAN operations on multiple devices in 1 SSH session per device",
    description=(
        "Apply N VLAN operations (create / rename / delete, in any mix) to M "
        "devices. Each device gets **one** background job that runs every "
        "operation in a **single** SSH session -- instead of N jobs with N "
        "sessions. Response carries a `group_job_id` and one job entry per "
        "device. Per-entry no-op detection still applies (e.g. a create for "
        "a VLAN that already has the same name is skipped). If the batch "
        "fails partway, rollback attempts to restore every entry that did "
        "change. A batch containing at least one deletion requires admin "
        "role on every target device; create/rename-only batches accept "
        "operator role."
    ),
)
def batch_vlans(
    data: VLANBatchRequest,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import group_operation_runner

    recursos: list[VLAN] = []
    try:
        for change in data.changes:
            if change.eliminar:
                entidad = VLAN(vlan_id=change.vlan_id, eliminar=True)
            else:
                entidad = VLAN(vlan_id=change.vlan_id, name=change.name or "")
                entidad.validar()
            recursos.append(entidad)
    except ValueError as e:
        raise ValidationError(str(e))

    devices_by_name = {name: require_device(name) for name in data.devices}
    # A batch that contains any deletion requires admin on every device --
    # matches the single-endpoint policy (DELETE /vlans/{id} is admin, POST/
    # PATCH are operator); the strictest role in the batch wins.
    any_delete = any(r.eliminar for r in recursos)
    min_role = "admin" if any_delete else "operator"
    _authz_devices(scope, data.devices, min_role=min_role, resolved_by_name=devices_by_name)

    # 1 job per device -- inside each device's job, all N recursos run in
    # the same SSH session via Orquestador.ejecutar_lote(). Same group_job_id
    # for every device so the UI can track the whole batch as one operation.
    import uuid
    group_job_id = str(uuid.uuid4())
    job_entries: list[dict] = []
    for device_name in data.devices:
        _, entry = group_operation_runner.encolar_lote(
            recursos, device_name, current_user["username"], group_job_id=group_job_id,
        )
        job_entries.append(entry)
    return ok({"group_job_id": group_job_id, "jobs": job_entries})


@router.patch(
    "/{vlan_id}",
    status_code=202,
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
    from app.composition import group_operation_runner

    try:
        entidad = VLAN(vlan_id=vlan_id, name=data.description)  # __post_init__ valida vlan_id
        entidad.validar()  # name -- __post_init__ no lo valida (Fase 2), ver nota en create_vlan()
    except ValueError as e:
        raise ValidationError(str(e))
    devices_by_name = {name: require_device(name) for name in data.devices}
    _authz_devices(scope, data.devices, min_role="operator", resolved_by_name=devices_by_name)
    group_job_id, jobs = group_operation_runner.encolar(entidad, data.devices, current_user["username"])
    return ok({"group_job_id": group_job_id, "jobs": jobs})
