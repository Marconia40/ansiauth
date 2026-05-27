import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

from app.core import authz
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
    current_user: dict = Depends(require_role("observer")),
):
    from app.services import device_locks

    if devices:
        authz.ensure_devices_allowed(current_user, devices)
        result = {}
        for dev in devices:
            try:
                with device_locks.acquire(dev, timeout=10):
                    result[dev] = [v.to_dict() for v in vlan_service.get_vlans(dev)]
            except TimeoutError:
                raise HTTPException(
                    status_code=503,
                    detail={"status": "device_busy", "device": dev, "message": "Device is busy with another operation, retry shortly"},
                )
            except ValueError as e:
                raise NotFoundError(str(e))
            except RuntimeError as e:
                raise DeviceExecutionError(str(e))
        return {"success": True, "data": result}

    if device is None and vlan_service.EXECUTION_MODE != "mock":
        raise ValidationError("'device' query parameter is required")

    if device is not None:
        authz.ensure_device_allowed(current_user, device)

    try:
        if device is not None:
            with device_locks.acquire(device, timeout=10):
                data = [v.to_dict() for v in vlan_service.get_vlans(device)]
        else:
            data = [v.to_dict() for v in vlan_service.get_vlans(device)]
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail={"status": "device_busy", "device": device, "message": "Device is busy with another operation, retry shortly"},
        )
    except ValueError as e:
        raise NotFoundError(str(e))
    except RuntimeError as e:
        raise DeviceExecutionError(str(e))
    return {"success": True, "data": data}


@router.post(
    "/",
    summary="Create VLAN",
    description=(
        "Create a new VLAN on one or more devices via Ansible. "
        "Each device gets its own background job — the response contains a job ID per device. "
        "Poll `GET /api/v1/jobs/{job_id}` for execution status. "
        "Requires operator role or higher."
    ),
)
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
    authz.ensure_devices_allowed(current_user, vlan.devices)
    jobs, group_job_id = vlan_execution_service.enqueue_create_jobs(vlan, current_user["username"], background_tasks, _RETRY_BASE_DELAY)
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}


@router.delete(
    "/{vlan_id}",
    summary="Delete VLAN",
    description=(
        "Remove a VLAN from one or more devices via Ansible. "
        "Returns a job ID per device. Reserved VLANs (1, 1002–1005) cannot be deleted. "
        "Requires admin role."
    ),
)
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
    authz.ensure_devices_allowed(current_user, data.devices)
    jobs, group_job_id = vlan_execution_service.enqueue_delete_jobs(vlan_id, data.devices, current_user["username"], background_tasks, _RETRY_BASE_DELAY)
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}


@router.patch(
    "/{vlan_id}",
    summary="Update VLAN",
    description=(
        "Update the description of an existing VLAN on one or more devices. "
        "Returns a job ID per device. Requires operator role or higher."
    ),
)
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
    authz.ensure_devices_allowed(current_user, data.devices)
    jobs, group_job_id = vlan_execution_service.enqueue_update_jobs(vlan_id, data, current_user["username"], background_tasks, _RETRY_BASE_DELAY)
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}
