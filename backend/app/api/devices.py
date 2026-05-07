import logging

from fastapi import APIRouter, Depends

from app.core.dependencies import require_role
from app.core.exceptions import NotFoundError, ValidationError
from app.schemas.device import DeviceCreate, DevicePublic
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
    ).model_dump()


@router.get("/")
def list_devices(current_user: dict = Depends(require_role("observer"))):
    return {"success": True, "data": [_to_public(d) for d in device_service.get_devices()]}


@router.get("/{name}")
def get_device(name: str, current_user: dict = Depends(require_role("observer"))):
    device = device_service.get_device(name)
    if not device:
        raise NotFoundError(f"Device '{name}' not found")
    return {"success": True, "data": _to_public(device)}


@router.post("/")
def create_device(data: DeviceCreate, current_user: dict = Depends(require_role("admin"))):
    try:
        device = device_service.create_device(
            name=data.name,
            host=data.host,
            vendor=data.vendor,
            platform=data.platform,
            username=data.username,
            password=data.password,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    audit_service.log_action(
        user=current_user["username"],
        action="create_device",
        resource="device",
        details={"id": device.id, "name": device.name, "host": device.host, "vendor": device.vendor},
    )
    return {"success": True, "data": _to_public(device)}


@router.delete("/{name}")
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
