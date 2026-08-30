import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.exceptions import NotFoundError, ValidationError
from app.core.scope import obtener_scope, require_authenticated, require_scope, resolver_site_group
from app.models.audit import AuditRecord
from app.models.visibility_scope import VisibilityScope
from app.schemas.device import DeviceCreate, DeviceMove, DevicePublic, DeviceUpdate

logger = logging.getLogger(__name__)
router = APIRouter()


_ROLE_LEVEL = {"observer": 1, "operator": 2, "admin": 3, "super-admin": 99}


def _to_public(device) -> dict:
    return DevicePublic(
        id=device.id,
        name=device.name,
        host=device.host,
        vendor=device.vendor,
        platform=device.platform,
        username=device.username,
        site_id=device.site_id,
        site_name=device.site_name,
        device_group_id=device.device_group_id,
        device_group_name=device.device_group_name,
    ).model_dump()


@router.get(
    "/",
    summary="List devices",
    description="Return all registered network devices. Credentials are never included in responses. Requires observer role or higher.",
)
def list_devices(
    site_id: int | None = None,
    device_group_id: int | None = None,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory

    devices = inventory.list(scope, site_id=site_id, device_group_id=device_group_id)
    return {"success": True, "data": [_to_public(d) for d in devices]}


@router.get(
    "/{name}",
    summary="Get device",
    description="Return a single device by its unique name. Requires observer role or higher.",
)
def get_device(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory

    device = inventory.get(name)
    if not device:
        raise NotFoundError(f"Device '{name}' not found")
    resolved = resolver_site_group(name, "device")
    role = scope.rol_para(*resolved) if resolved is not None else None
    if role is None:
        raise NotFoundError(f"Device '{name}' not found")
    return {"success": True, "data": _to_public(device)}


@router.post(
    "/",
    summary="Register device",
    description=(
        "Add a new network device to the inventory. "
        "The password is encrypted at rest using AES-256 and never returned in responses. "
        "Requires admin on the target site."
    ),
)
def create_device(
    data: DeviceCreate,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory

    # site_id lives in the body, not the path — the require_scope dep
    # can't pre-resolve it, so this stays imperative.
    role = scope.rol_para(data.site_id, None) if data.site_id else None
    if not current_user.get("is_system_admin") and role != "admin":
        raise HTTPException(
            status_code=403,
            detail=(
                f"register_device requires admin on site {data.site_id} "
                f"(got {role or 'none'})"
            ),
        )
    try:
        device = inventory.register(
            name=data.name,
            host=data.host,
            vendor=data.vendor,
            platform=data.platform,
            username=data.username,
            password=data.password,
            site_id=data.site_id,
            device_group_id=data.device_group_id,
            actor=current_user,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    return {"success": True, "data": _to_public(device)}


@router.put(
    "/{name}",
    summary="Update device",
    description=(
        "Update connection details for a registered device. "
        "All fields are optional — only provided fields are changed. "
        "Requires admin on the device."
    ),
)
def update_device(
    name: str,
    data: DeviceUpdate,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import audit_repository, device_repository
    from app.services.secret_service import vault

    provided = data.model_dump(exclude_unset=True)
    if not provided:
        raise ValidationError("No fields provided for update")
    resolved = resolver_site_group(name, "device")
    role = scope.rol_para(*resolved) if resolved is not None else None
    if not current_user.get("is_system_admin") and role != "admin":
        raise HTTPException(
            status_code=403,
            detail=(
                f"edit_device requires admin on device '{name}' "
                f"(got {role or 'none'})"
            ),
        )
    device = device_repository.get(name)
    if device is None:
        raise NotFoundError(f"Device '{name}' not found")
    try:
        device.actualizar(
            host=data.host, vendor=data.vendor, platform=data.platform,
            username=data.username, password=data.password, vault=vault,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    device = device_repository.add(device)
    audit_fields = {k: v for k, v in provided.items() if k != "password"}
    audit_repository.append(AuditRecord(
        user=current_user["username"],
        action="update_device",
        resource="device",
        details={"name": name, "updated_fields": audit_fields},
        device=name,
    ))
    return {"success": True, "data": _to_public(device)}


@router.post(
    "/{name}/move",
    summary="Move device to a group",
    description=(
        "Change a device's owning group. Same-site moves require operator on the "
        "device; cross-site moves require admin on both sides. Per D8, passing "
        "`device_group_id: null` moves the device to its current site's Default "
        "group (the device-level 'remove from group' action)."
    ),
    status_code=200,
)
def move_device(
    name: str,
    body: DeviceMove,
    current_user: dict = Depends(require_scope("move_device")),
):
    from app.composition import inventory

    device = inventory.move(name, body.device_group_id, actor=current_user)
    return {"success": True, "data": _to_public(device)}


@router.post(
    "/{name}/save",
    summary="Save device configuration",
    description=(
        "Persist the running configuration to flash on the target device. "
        "Runs asynchronously — poll the returned job_id for the result. "
        "Only supported for vendors with a save_config implementation (e.g. Huawei VRP). "
        "Requires operator role or higher on the device."
    ),
)
def save_device_config(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory

    device = inventory.get(name)
    if not device:
        raise NotFoundError(f"Device '{name}' not found")
    resolved = resolver_site_group(name, "device")
    role = scope.rol_para(*resolved) if resolved is not None else None
    if _ROLE_LEVEL.get(role or "", 0) < _ROLE_LEVEL["operator"]:
        raise HTTPException(
            status_code=403,
            detail=(
                f"write_device_config requires operator on device '{name}' "
                f"(got {role or 'none'})"
            ),
        )
    # "Guardar configuración" no es un RecursoGestionable (Fase 5) ni una
    # operación de Inventory (Fase 6, cap de 5 métodos) -- Fase 7 le agregó
    # su propio camino mínimo (Orquestador.ejecutar_comando() +
    # app.tasks.guardar_config_task), reemplazando
    # vlan_execution_service.enqueue_save_job() (que además ya estaba roto
    # en producción: despachaba a un task Celery que ningún worker real
    # tenía registrado desde Fase 5/A5, ver docstring de
    # guardar_config_task).
    from app.composition import job_repository
    from app.models.job import Job
    from app.tasks import guardar_config_task

    job = Job(operation="guardar_config", device=name)
    job_repository.add(job)
    guardar_config_task.delay(name, current_user["username"], job.job_id)
    return {"success": True, "data": {"device": name, "job_id": job.job_id, "status": job.status}}


@router.delete(
    "/{name}",
    summary="Delete device",
    description="Permanently remove a device from the inventory. Requires admin on the device.",
)
def delete_device(
    name: str,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import inventory

    resolved = resolver_site_group(name, "device")
    role = scope.rol_para(*resolved) if resolved is not None else None
    if not current_user.get("is_system_admin") and role != "admin":
        raise HTTPException(
            status_code=403,
            detail=f"delete_device requires admin on device '{name}' (got {role or 'none'})",
        )
    inventory.deregister(name, actor=current_user)
    return {"success": True, "data": {"name": name}}
