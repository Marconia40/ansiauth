from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core import authz
from app.core.dependencies import require_role
from app.core.exceptions import DeviceExecutionError, NotFoundError, ValidationError
from app.schemas.port import PortRead  # noqa: F401 — exported via OpenAPI components
from app.services import port_service

logger = logging.getLogger(__name__)
router = APIRouter()


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
        response = port_service.list_ports(target)
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
