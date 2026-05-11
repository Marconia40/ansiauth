import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from app.core.dependencies import require_role
from app.core.exceptions import DeviceExecutionError, NotFoundError, ValidationError
from app.schemas.vlan import VLANCreate, VLANDelete, VLANUpdate
from app.services import device_service, vlan_execution_service, vlan_service
from app.services.vlan_execution_service import _capture_pre_state_vlan  # noqa: F401 — re-exported for test monkeypatching
from app.validators import vlan_validator

logger = logging.getLogger(__name__)
router = APIRouter()

# Controls the base wait between retries (1s × 2^attempt). Kept here so tests
# can monkeypatch it via app.api.vlans._RETRY_BASE_DELAY.
_RETRY_BASE_DELAY: float = 1.0


@router.get("/")
def get_vlans(
    device: str | None = None,
    devices: list[str] | None = Query(default=None),
    current_user: dict = Depends(require_role("observer")),
):
    if devices:
        result = {}
        for dev in devices:
            try:
                result[dev] = vlan_service.get_vlans(dev)
            except ValueError as e:
                raise NotFoundError(str(e))
            except RuntimeError as e:
                raise DeviceExecutionError(str(e))
        return {"success": True, "data": result}
    if device is None and vlan_service.EXECUTION_MODE != "mock":
        raise ValidationError("'device' query parameter is required")
    try:
        data = vlan_service.get_vlans(device)
    except ValueError as e:
        raise NotFoundError(str(e))
    except RuntimeError as e:
        raise DeviceExecutionError(str(e))
    return {"success": True, "data": data}


@router.post("/")
def create_vlan(
    vlan: VLANCreate,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_role("operator")),
):
    try:
        vlan_validator.validate_vlan_id_range(vlan.vlan_id)
        vlan_validator.validate_vlan_not_reserved(vlan.vlan_id)
        vlan_validator.validate_vlan_name(vlan.name)
    except ValueError as e:
        raise ValidationError(str(e))
    for dev_name in vlan.devices:
        if not device_service.get_device(dev_name):
            raise NotFoundError(f"Device '{dev_name}' not found")
    jobs = vlan_execution_service.enqueue_create_jobs(vlan, current_user["username"], background_tasks, _RETRY_BASE_DELAY)
    return {"success": True, "jobs": jobs}


@router.delete("/{vlan_id}")
def delete_vlan(
    vlan_id: int,
    data: VLANDelete,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_role("admin")),
):
    try:
        vlan_validator.validate_vlan_id_range(vlan_id)
        vlan_validator.validate_vlan_not_reserved(vlan_id)
    except ValueError as e:
        raise ValidationError(str(e))
    for dev_name in data.devices:
        if not device_service.get_device(dev_name):
            raise NotFoundError(f"Device '{dev_name}' not found")
    jobs = vlan_execution_service.enqueue_delete_jobs(vlan_id, data.devices, current_user["username"], background_tasks, _RETRY_BASE_DELAY)
    return {"success": True, "jobs": jobs}


@router.patch("/{vlan_id}")
def update_vlan(
    vlan_id: int,
    data: VLANUpdate,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_role("operator")),
):
    try:
        vlan_validator.validate_vlan_id_range(vlan_id)
        vlan_validator.validate_vlan_not_reserved(vlan_id)
        vlan_validator.validate_description(data.description)
    except ValueError as e:
        raise ValidationError(str(e))
    for dev_name in data.devices:
        if not device_service.get_device(dev_name):
            raise NotFoundError(f"Device '{dev_name}' not found")
    jobs = vlan_execution_service.enqueue_update_jobs(vlan_id, data, current_user["username"], background_tasks, _RETRY_BASE_DELAY)
    return {"success": True, "jobs": jobs}
