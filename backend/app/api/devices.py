import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.exceptions import NotFoundError, ValidationError
from app.core.scope import obtener_scope, require_authenticated, require_scope, resolver_site_group
from app.models.visibility_scope import VisibilityScope
from app.schemas.device import DeviceCreate, DeviceMove, DevicePublic, DeviceUpdate

logger = logging.getLogger(__name__)
router = APIRouter()


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
    from app.composition import device_repository, event_dispatcher
    from app.models.domain_event import DomainEvent
    from app.services.secret_vault import vault

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
    # Antes llamaba audit_repository.append(AuditRecord(...)) directo --
    # único endpoint del ciclo de vida de Device que se saltaba
    # EventDispatcher (register()/move()/deregister() en Inventory ya
    # despachan DomainEvent) -- corrección real, FINAL_ARCHITECTURE.md §6:
    # si mañana se agrega un 2do EventListener, esta escritura ahora
    # participa igual que las otras 3.
    event_dispatcher.despachar([DomainEvent(
        "update_device", device, device, current_user["username"],
        {"name": name, "updated_fields": audit_fields},
    )])
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
