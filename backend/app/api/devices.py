import logging

from fastapi import APIRouter, BackgroundTasks, Depends

from app.core.dependencies import require_role
from app.core.exceptions import NotFoundError, ValidationError
from app.schemas.device import DeviceCreate, DevicePublic, DeviceUpdate
from app.services import audit_service, device_service

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
    ).model_dump()


@router.get(
    "/",
    summary="List devices",
    description="Return all registered network devices. Credentials are never included in responses. Requires observer role or higher.",
)
def list_devices(current_user: dict = Depends(require_role("observer"))):
    return {"success": True, "data": [_to_public(d) for d in device_service.get_devices()]}


@router.get(
    "/{name}",
    summary="Get device",
    description="Return a single device by its unique name. Requires observer role or higher.",
)
def get_device(name: str, current_user: dict = Depends(require_role("observer"))):
    device = device_service.get_device(name)
    if not device:
        raise NotFoundError(f"Device '{name}' not found")
    return {"success": True, "data": _to_public(device)}


@router.post(
    "/",
    summary="Register device",
    description=(
        "Add a new network device to the inventory. "
        "The password is encrypted at rest using AES-256 and never returned in responses. "
        "Requires admin role."
    ),
)
def create_device(data: DeviceCreate, current_user: dict = Depends(require_role("admin"))):
    try:
        device = device_service.create_device(
            name=data.name,
            host=data.host,
            vendor=data.vendor,
            platform=data.platform,
            username=data.username,
            password=data.password,
            site_id=data.site_id,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    audit_service.log_action(
        user=current_user["username"],
        action="create_device",
        resource="device",
        details={
            "id": device.id,
            "name": device.name,
            "host": device.host,
            "vendor": device.vendor,
            "site_id": device.site_id,
        },
    )
    return {"success": True, "data": _to_public(device)}


@router.put(
    "/{name}",
    summary="Update device",
    description=(
        "Update connection details for a registered device. "
        "All fields are optional — only provided fields are changed. "
        "Requires admin role."
    ),
)
def update_device(name: str, data: DeviceUpdate, current_user: dict = Depends(require_role("admin"))):
    # Differentiate "not provided" from "explicitly set to null" — needed so callers
    # can clear site_id via {"site_id": null}.
    provided = data.model_dump(exclude_unset=True)
    if not provided:
        raise ValidationError("No fields provided for update")
    try:
        # Pass the service sentinel only when site_id was actually included in the body.
        site_id_kwarg = {"site_id": data.site_id} if "site_id" in provided else {}
        device = device_service.update_device(
            name=name,
            host=data.host,
            vendor=data.vendor,
            platform=data.platform,
            username=data.username,
            password=data.password,
            **site_id_kwarg,
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
    "/{name}/save",
    summary="Save device configuration",
    description=(
        "Persist the running configuration to flash on the target device. "
        "Runs asynchronously — poll the returned job_id for the result. "
        "Only supported for vendors with a save_config implementation (e.g. Huawei VRP). "
        "Requires operator role or higher."
    ),
)
def save_device_config(
    name: str,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_role("operator")),
):
    device = device_service.get_device(name)
    if not device:
        raise NotFoundError(f"Device '{name}' not found")
    from app.services.vlan_execution_service import enqueue_save_job
    entry = enqueue_save_job(name, current_user["username"], background_tasks)
    return {"success": True, "data": entry}


@router.delete(
    "/{name}",
    summary="Delete device",
    description="Permanently remove a device from the inventory. Requires admin role.",
)
def delete_device(name: str, current_user: dict = Depends(require_role("admin"))):
    device = device_service.delete_device(name)
    if not device:
        raise NotFoundError(f"Device '{name}' not found")
    audit_service.log_action(
        user=current_user["username"],
        action="delete_device",
        resource="device",
        details={"name": name},
    )
    return {"success": True, "data": {"name": name}}
