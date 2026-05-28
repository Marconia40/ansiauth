from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from app.core import authz
from app.core.dependencies import require_role
from app.core.exceptions import (
    DeviceExecutionError,
    NotFoundError,
    UnsupportedVendorError,
    ValidationError,
)
from app.schemas.port import PortDescriptionUpdateRequest, PortRead  # noqa: F401 — exported via OpenAPI components
from app.services import device_service, port_execution_service, port_service
from app.validators import port_validator

logger = logging.getLogger(__name__)
router = APIRouter()

# Controls the base wait between retries (1s × 2^attempt). Kept module-local
# so tests can monkeypatch it via app.api.ports._RETRY_BASE_DELAY (mirrors the
# VLAN endpoint's monkeypatch surface).
_RETRY_BASE_DELAY: float = 1.0


def _capture_pre_state_port_description(interface: str, device: str) -> dict:
    """Re-export the execution-layer pre-state capture so tests can
    monkeypatch a single attachment point.  Mirrors the VLAN module's
    ``_capture_pre_state_vlan`` indirection."""
    return port_execution_service._capture_pre_state_description(interface, device)


@router.get(
    "/",
    summary="List ports",
    description=(
        "Retrieve the normalized port inventory of a single device. "
        "Pass `device=<name>` as a query parameter.  Returns interface "
        "name, description, admin and operational state, switchport mode, "
        "access / trunk VLAN information, and (when exposed by the device) "
        "PoE / speed / duplex.  Read-only — no configuration changes. "
        "Requires observer role or higher; site-scoped users may only "
        "query devices in their allowed sites."
    ),
)
def list_ports(
    device: str | None = None,
    current_user: dict = Depends(require_role("observer")),
):
    """Return the port inventory of *device*.

    Read-only endpoint.  Errors are mapped to standard project codes:

    * ``400`` — ``device`` query parameter missing in non-mock mode.
    * ``403`` — caller's allowed sites do not include this device.
    * ``404`` — device unknown.
    * ``500`` — device-side execution / parser failure.
    * ``503`` — device is busy with another in-flight operation.
    """
    if device is None and port_service.EXECUTION_MODE != "mock":
        raise ValidationError("'device' query parameter is required")

    if device is not None:
        authz.ensure_device_allowed(current_user, device)

    target = device if device is not None else "mock_device"
    try:
        # Match the VLAN read path: API layer acquires the lock with a short
        # timeout so concurrent reads can fail fast when a write is in flight.
        from app.services import device_locks

        with device_locks.acquire(target, timeout=10):
            response = port_service.list_ports(target)
    except UnsupportedVendorError as exc:
        # Detailed (vendor / platform) details stay in the server log via the
        # dispatcher's WARNING entry; clients receive a controlled message
        # that does not leak backend internals.
        logger.info(
            "Port management requested on unsupported vendor: device=%s vendor=%s platform=%s",
            target, exc.vendor, exc.platform,
        )
        raise HTTPException(
            status_code=501,
            detail={
                "error_code": "VENDOR_NOT_SUPPORTED",
                "message": "Port management is not yet supported for this vendor.",
            },
        )
    except TimeoutError:
        # Surfaced when the per-device lock cannot be acquired — somebody else
        # is mid-operation against the same device.  Mirror the VLAN endpoint's
        # 503 + payload so the frontend can show a consistent message.
        raise HTTPException(
            status_code=503,
            detail={
                "status": "device_busy",
                "device": target,
                "message": "Device is busy with another operation, retry shortly",
            },
        )
    except ValueError as exc:
        # ``port_service`` raises ValueError for unknown device IDs.
        raise NotFoundError(str(exc))
    except RuntimeError as exc:
        # Playbook failure or parser failure — caller can retry but the root
        # cause is on the device side.
        raise DeviceExecutionError(str(exc))

    payload = {
        "device": response.device,
        "vendor": response.vendor,
        "count": len(response.ports),
        "ports": [PortRead(**p.to_dict()) for p in response.ports],
    }
    return {"success": True, "data": payload}


@router.patch(
    "/description",
    summary="Update port description",
    description=(
        "Update the description of a single interface on a device.  An "
        "empty description clears the description (`undo description` on "
        "Huawei VRP, `no description` on Cisco IOS).  Executed "
        "asynchronously: the response carries a `group_job_id` and per-"
        "device job entry the frontend can poll via "
        "`GET /api/v1/jobs/{job_id}` and `GET /api/v1/group-jobs/{id}`.  "
        "Pre-state is captured for rollback — if the device-side change "
        "fails, the original description is restored automatically.  "
        "Requires operator role or higher; site-scoped users may only "
        "target devices in their allowed sites."
    ),
)
def update_port_description(
    data: PortDescriptionUpdateRequest,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_role("operator")),
):
    """Schedule a port-description update on a single device."""
    try:
        port_validator.validate_interface_name(data.interface)
        port_validator.validate_description(data.description)
    except ValueError as exc:
        raise ValidationError(str(exc))

    if not device_service.get_device(data.device):
        raise NotFoundError(f"Device '{data.device}' not found")
    authz.ensure_device_allowed(current_user, data.device)

    # Verify the device's vendor has an update_port_description driver
    # implementation before we enqueue a job that would only fail at runtime.
    # The dispatcher raises ``UnsupportedVendorError`` for vendors with no
    # port driver; we then probe the driver class for an override of the
    # base method so the API can return the friendly 501 immediately.
    try:
        device_obj = device_service.get_device(data.device)
        from app.services.vendors.dispatcher import get_port_driver
        from app.services.vendors.port_driver_base import BasePortDriver
        driver = get_port_driver(device_obj)
    except UnsupportedVendorError as exc:
        logger.info(
            "Port description update requested on unsupported vendor: device=%s vendor=%s platform=%s",
            data.device, exc.vendor, exc.platform,
        )
        raise HTTPException(
            status_code=501,
            detail={
                "error_code": "VENDOR_NOT_SUPPORTED",
                "message": "Port management is not yet supported for this vendor.",
            },
        )
    if type(driver).update_port_description is BasePortDriver.update_port_description:
        # Driver inherits the base default (NotImplementedError stub).  Treat
        # the same way as an unsupported vendor — never surface the raw
        # NotImplementedError to the client.
        logger.info(
            "Port description update unimplemented on driver=%s for device=%s",
            type(driver).__name__, data.device,
        )
        raise HTTPException(
            status_code=501,
            detail={
                "error_code": "VENDOR_NOT_SUPPORTED",
                "message": "Port description update is not yet supported for this vendor.",
            },
        )

    jobs, group_job_id = port_execution_service.enqueue_update_description_job(
        interface=data.interface,
        description=data.description,
        device=data.device,
        username=current_user["username"],
        background_tasks=background_tasks,
        retry_base_delay=_RETRY_BASE_DELAY,
    )
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}
