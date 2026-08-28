import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core import authz
from app.core.config import settings
from app.core.dependencies import require_role
from app.core.exceptions import DeviceExecutionError, NotFoundError, ValidationError
from app.core.scope import require_authenticated
from app.schemas.vlan import VLANCreate, VLANDelete, VLANUpdate
from app.services import device_service, vlan_execution_service, vlan_service
from app.services.vlan_execution_service import _capture_pre_state_vlan  # noqa: F401 — re-exported for test monkeypatching
from app.validators import vlan_validator

logger = logging.getLogger(__name__)
router = APIRouter()

# Controls the base wait between retries (1s × 2^attempt). Kept here so tests
# can monkeypatch it via app.api.vlans._RETRY_BASE_DELAY.
_RETRY_BASE_DELAY: float = 1.0


def _authz_devices(user: dict, device_names, *, min_role: str) -> None:
    """Enforce read/write access to every device in *device_names*.

    MSP: Phase 3 (ESC-3) — under the strict-hierarchy flag this uses
    ``effective_role`` (per-scope grants). Under flag-off it falls back to
    the legacy ``ensure_devices_allowed`` path so pre-MSP test fixtures with
    site_id=NULL seed devices keep working; T3.5 snapshot-diff proves the two
    return identical result sets once the MSP flag flips on in staging.
    """
    if not settings.MSP_STRICT_HIERARCHY:
        _LVL_LEGACY = {"observer": 1, "operator": 2, "admin": 3, "super-admin": 4}
        if _LVL_LEGACY.get(user.get("role") or "", 0) < _LVL_LEGACY[min_role]:
            raise HTTPException(
                status_code=403,
                detail="Insufficient permissions",
            )
        authz.ensure_devices_allowed(user, device_names)
        return
    from app.services.effective_role import effective_role
    from app.db.session import get_session
    _LVL = {"observer": 1, "operator": 2, "admin": 3, "super-admin": 99}
    threshold = _LVL[min_role]
    with get_session() as session:
        for name in device_names:
            role = effective_role(session, user, "device", name)
            if _LVL.get(role or "", 0) < threshold:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"VLAN op on device '{name}' requires role >= {min_role} "
                        f"(got {role or 'none'})"
                    ),
                )


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
):
    from app.services import device_locks

    if devices:
        _authz_devices(current_user, devices, min_role="observer")
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
        _authz_devices(current_user, [device], min_role="observer")

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
        "Requires operator role or higher on every target device."
    ),
)
def create_vlan(
    vlan: VLANCreate,
    current_user: dict = Depends(require_authenticated),
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
    # require_scope above authorized the first device; check every remaining
    # target so no half-successful batch slips through.
    _authz_devices(current_user, vlan.devices, min_role="operator")
    jobs, group_job_id = vlan_execution_service.enqueue_create_jobs(vlan, current_user["username"], _RETRY_BASE_DELAY)
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}


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
):
    try:
        vlan_validator.validate_vlan_id_range(vlan_id)
        vlan_validator.validate_vlan_not_reserved(vlan_id)
    except ValueError as e:
        raise ValidationError(str(e))
    for dev_name in data.devices:
        if not device_service.get_device(dev_name):
            raise NotFoundError(f"Device '{dev_name}' not found")
    # Legacy delete_vlan required admin; ESC-3 keeps that gate — DELETE is
    # coarser-grained than create/update (harder to reverse) so it stays
    # admin-only per device.
    _authz_devices(current_user, data.devices, min_role="admin")
    jobs, group_job_id = vlan_execution_service.enqueue_delete_jobs(vlan_id, data.devices, current_user["username"], _RETRY_BASE_DELAY)
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}


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
    _authz_devices(current_user, data.devices, min_role="operator")
    jobs, group_job_id = vlan_execution_service.enqueue_update_jobs(vlan_id, data, current_user["username"], _RETRY_BASE_DELAY)
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}
