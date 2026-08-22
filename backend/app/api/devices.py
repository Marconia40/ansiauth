import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.exceptions import NotFoundError, ValidationError
from app.core.scope import require_authenticated, require_scope
from app.db.session import get_session
from app.schemas.device import DeviceCreate, DeviceMove, DevicePublic, DeviceUpdate
from app.services import audit_service, device_service
from app.services.effective_role import effective_role
from app.services.inventory_service import Inventory

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
):
    devices = Inventory().list(current_user, site_id=site_id, device_group_id=device_group_id)
    return {"success": True, "data": [_to_public(d) for d in devices]}


@router.get(
    "/{name}",
    summary="Get device",
    description="Return a single device by its unique name. Requires observer role or higher.",
)
def get_device(name: str, current_user: dict = Depends(require_authenticated)):
    device = device_service.get_device(name)
    if not device:
        raise NotFoundError(f"Device '{name}' not found")
    with get_session() as session:
        role = effective_role(session, current_user, "device", name)
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
):
    # The scope dep can't be swapped at runtime, and `site_id` lives in the
    # body — run effective_role inline for the auth check.
    with get_session() as session:
        role = effective_role(session, current_user, "site", data.site_id) if data.site_id else None
    if not current_user.get("is_system_admin") and role != "admin":
        raise HTTPException(
            status_code=403,
            detail=(
                f"register_device requires admin on site {data.site_id} "
                f"(got {role or 'none'})"
            ),
        )
    try:
        device = Inventory().register(
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
):
    provided = data.model_dump(exclude_unset=True)
    if not provided:
        raise ValidationError("No fields provided for update")
    with get_session() as session:
        role = effective_role(session, current_user, "device", name)
    if not current_user.get("is_system_admin") and role != "admin":
        raise HTTPException(
            status_code=403,
            detail=(
                f"edit_device requires admin on device '{name}' "
                f"(got {role or 'none'})"
            ),
        )
    try:
        device = device_service.update_device(
            name=name,
            host=data.host,
            vendor=data.vendor,
            platform=data.platform,
            username=data.username,
            password=data.password,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    audit_fields = {k: v for k, v in provided.items() if k != "password"}
    audit_service.log_action(
        user=current_user["username"],
        action="update_device",
        resource="device",
        details={"name": name, "updated_fields": audit_fields},
    )
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
    device = Inventory().move(name, body.device_group_id, actor=current_user)
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
):
    device = device_service.get_device(name)
    if not device:
        raise NotFoundError(f"Device '{name}' not found")
    with get_session() as session:
        role = effective_role(session, current_user, "device", name)
    if _ROLE_LEVEL.get(role or "", 0) < _ROLE_LEVEL["operator"]:
        raise HTTPException(
            status_code=403,
            detail=(
                f"write_device_config requires operator on device '{name}' "
                f"(got {role or 'none'})"
            ),
        )
    from app.services.vlan_execution_service import enqueue_save_job
    entry = enqueue_save_job(name, current_user["username"])
    return {"success": True, "data": entry}


@router.delete(
    "/{name}",
    summary="Delete device",
    description="Permanently remove a device from the inventory. Requires admin on the device.",
)
def delete_device(name: str, current_user: dict = Depends(require_authenticated)):
    with get_session() as session:
        role = effective_role(session, current_user, "device", name)
    if not current_user.get("is_system_admin") and role != "admin":
        raise HTTPException(
            status_code=403,
            detail=f"delete_device requires admin on device '{name}' (got {role or 'none'})",
        )
    Inventory().deregister(name, actor=current_user)
    return {"success": True, "data": {"name": name}}
