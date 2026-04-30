import logging

from fastapi import APIRouter, Depends

from app.core.dependencies import require_role
from app.core.exceptions import NotFoundError
from app.models.device import Device
from app.services import audit_service, device_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/")
def get_devices(current_user: dict = Depends(require_role("observer"))):
    return {"success": True, "data": [d.model_dump() for d in device_service.get_devices()]}


@router.post("/")
def create_device(device: Device, current_user: dict = Depends(require_role("admin"))):
    created = device_service.create_device(device)
    audit_service.log_action(
        user=current_user["username"],
        action="create_device",
        resource="device",
        details={"id": device.id, "ip": device.ip, "type": device.type},
    )
    return {"success": True, "data": created.model_dump()}


@router.delete("/{device_id}")
def delete_device(device_id: str, current_user: dict = Depends(require_role("admin"))):
    device = device_service.delete_device(device_id)
    if not device:
        raise NotFoundError(f"Device '{device_id}' not found")
    audit_service.log_action(
        user=current_user["username"],
        action="delete_device",
        resource="device",
        details={"id": device_id},
    )
    return {"success": True, "data": {"id": device_id}}
