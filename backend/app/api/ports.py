from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core import authz
from app.core.config import settings
from app.core.scope import require_authenticated


def _authz_device(user: dict, device_name: str, *, min_role: str) -> None:
    """Enforce read/write access to *device_name*.

    MSP: Phase 3 (ESC-2) — under the strict-hierarchy flag uses per-scope
    ``effective_role``; under flag-off delegates to legacy
    ``ensure_device_allowed`` so pre-MSP fixtures (mock_device with
    site_id=NULL) keep working. T3.5 snapshot-diff proves the two return
    identical decisions on the same corpus once the flag flips on.
    """
    if not settings.MSP_STRICT_HIERARCHY:
        _LVL_LEGACY = {"observer": 1, "operator": 2, "admin": 3, "super-admin": 4}
        if _LVL_LEGACY.get(user.get("role") or "", 0) < _LVL_LEGACY[min_role]:
            raise HTTPException(
                status_code=403,
                detail="Insufficient permissions",
            )
        authz.ensure_device_allowed(user, device_name)
        return
    from app.services.effective_role import effective_role
    from app.db.session import get_session
    _LVL = {"observer": 1, "operator": 2, "admin": 3, "super-admin": 99}
    with get_session() as session:
        role = effective_role(session, user, "device", device_name)
    if _LVL.get(role or "", 0) < _LVL[min_role]:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Port op on device '{device_name}' requires role >= {min_role} "
                f"(got {role or 'none'})"
            ),
        )
from app.core.exceptions import (
    DeviceExecutionError,
    NotFoundError,
    UnsupportedVendorError,
    ValidationError,
)
from app.schemas.port import (
    PortAccessVlanUpdateRequest,
    PortAdminStateUpdateRequest,
    PortConfigureRequest,
    PortDescriptionUpdateRequest,
    PortEnableRequest,
    PortShutdownRequest,
    PortTrunkVlansUpdateRequest,
    PortRead,  # noqa: F401 — exported via OpenAPI components
)
from app.services import device_service, port_config_service, port_execution_service, port_service
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


def _capture_pre_state_port_admin(interface: str, device: str) -> dict:
    """Re-export the admin-state pre-state capture (Step 2.2) so tests can
    monkeypatch a single attachment point."""
    return port_execution_service._capture_pre_state_admin(interface, device)


def _capture_pre_state_port_access_vlan(interface: str, device: str) -> dict:
    """Re-export the access-VLAN pre-state capture (Step 2.3) so tests can
    monkeypatch a single attachment point."""
    return port_execution_service._capture_pre_state_access_vlan(interface, device)


def _capture_pre_state_port_trunk_vlans(interface: str, device: str) -> dict:
    """Re-export the trunk-VLAN pre-state capture (Step 2.4) so tests can
    monkeypatch a single attachment point."""
    return port_execution_service._capture_pre_state_trunk_vlans(interface, device)


def _capture_pre_state_port_configure(interface: str, device: str) -> dict:
    """Re-export the configure pre-state capture (Step 3.3) so tests can
    monkeypatch a single attachment point."""
    return port_config_service._capture_pre_state_configure(interface, device)


def _capture_pre_state_port_shutdown(interface: str, device: str) -> dict:
    """Re-export the shutdown pre-state capture (Step 3.3) so tests can
    monkeypatch a single attachment point."""
    return port_config_service._capture_pre_state_shutdown(interface, device)


def _capture_pre_state_port_enable(interface: str, device: str) -> dict:
    """Re-export the enable pre-state capture (Step 3.3) so tests can
    monkeypatch a single attachment point."""
    return port_config_service._capture_pre_state_enable(interface, device)


def _check_device_not_locked(device_name: str) -> None:
    """Raise 409 immediately when the device is already held by another operation.

    Uses a non-blocking lock probe so the check itself has no side-effects.
    The caller proceeds to enqueue only when the device is currently free.
    A small TOCTOU window exists — if the device becomes busy between this
    check and the lock acquisition inside the service, the enqueue still
    succeeds (the service has its own 30-second acquisition window and will
    degrade gracefully on an extremely rare second collision).
    """
    from app.services import device_locks

    if device_locks.is_device_busy(device_name):
        raise HTTPException(
            status_code=409,
            detail={
                "error_code": "DEVICE_LOCKED",
                "message": (
                    f"Device '{device_name}' is busy with another operation — "
                    "retry shortly"
                ),
            },
        )


def _require_port_driver_with(method_name: str, device_name: str, current_user: dict):
    """Resolve the port driver for *device_name* and assert that the driver
    actually overrides *method_name*.  Returns the driver if everything is
    in order; raises ``HTTPException(501)`` with the friendly
    VENDOR_NOT_SUPPORTED envelope when the vendor is unknown or has only
    inherited the base default stub.

    Shared by the description and admin-state endpoints so both surface the
    same controlled error for unsupported vendors.
    """
    from app.services.vendors.dispatcher import get_port_driver
    from app.services.vendors.port_driver_base import BasePortDriver

    device_obj = device_service.get_device(device_name)
    # caller already verified existence + RBAC; we re-read for the dispatcher
    try:
        driver = get_port_driver(device_obj)
    except UnsupportedVendorError as exc:
        logger.info(
            "Port %s requested on unsupported vendor: device=%s vendor=%s platform=%s",
            method_name, device_name, exc.vendor, exc.platform,
        )
        raise HTTPException(
            status_code=501,
            detail={
                "error_code": "VENDOR_NOT_SUPPORTED",
                "message": "Port management is not yet supported for this vendor.",
            },
        )
    if getattr(type(driver), method_name) is getattr(BasePortDriver, method_name):
        logger.info(
            "Port %s unimplemented on driver=%s for device=%s",
            method_name, type(driver).__name__, device_name,
        )
        raise HTTPException(
            status_code=501,
            detail={
                "error_code": "VENDOR_NOT_SUPPORTED",
                "message": "This port operation is not yet supported for this vendor.",
            },
        )
    return driver


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
    current_user: dict = Depends(require_authenticated),
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

    # ESC-2: swap ensure_device_allowed for effective_role. Kept imperative
    # here because ``device`` is a query parameter, not path or body — the
    # require_scope resolver only reads path + body.
    if device is not None:
        _authz_device(current_user, device, min_role="observer")

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
    current_user: dict = Depends(require_authenticated),
):
    """Schedule a port-description update on a single device."""
    try:
        port_validator.validate_interface_name(data.interface)
        port_validator.validate_description(data.description)
    except ValueError as exc:
        raise ValidationError(str(exc))

    if not device_service.get_device(data.device):
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(current_user, data.device, min_role="operator")

    # Resolve the driver and verify the operation is supported for this
    # vendor.  Returns 501 with a controlled message when not — see
    # ``_require_port_driver_with`` for the exact gating.
    _require_port_driver_with("update_port_description", data.device, current_user)

    jobs, group_job_id = port_execution_service.enqueue_update_description_job(
        interface=data.interface,
        description=data.description,
        device=data.device,
        username=current_user["username"],
        retry_base_delay=_RETRY_BASE_DELAY,
    )
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}


@router.patch(
    "/admin-state",
    summary="Set port admin state",
    description=(
        "Administratively enable or disable a single interface.  "
        "``enabled=true`` runs ``undo shutdown`` (Huawei) / ``no shutdown`` "
        "(Cisco); ``enabled=false`` runs ``shutdown``.  Executed "
        "asynchronously: the response carries a ``group_job_id`` and per-"
        "device job entry the frontend can poll via "
        "``GET /api/v1/jobs/{job_id}`` and ``GET /api/v1/group-jobs/{id}``.  "
        "Pre-state is captured for rollback — if the device-side change "
        "fails, the original admin state is restored automatically.  "
        "Requires operator role or higher; site-scoped users may only "
        "target devices in their allowed sites."
    ),
)
def set_port_admin_state(
    data: PortAdminStateUpdateRequest,
    current_user: dict = Depends(require_authenticated),
):
    """Schedule an admin-state change on a single port."""
    try:
        port_validator.validate_interface_name(data.interface)
    except ValueError as exc:
        raise ValidationError(str(exc))

    if not device_service.get_device(data.device):
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(current_user, data.device, min_role="operator")

    _require_port_driver_with("set_port_admin_state", data.device, current_user)

    jobs, group_job_id = port_execution_service.enqueue_set_admin_state_job(
        interface=data.interface,
        enabled=data.enabled,
        device=data.device,
        username=current_user["username"],
        retry_base_delay=_RETRY_BASE_DELAY,
    )
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}


@router.patch(
    "/access-vlan",
    summary="Set port access VLAN",
    description=(
        "Assign an access VLAN to a single interface.  The port must already "
        "be in access mode; the orchestration layer verifies this via pre-state "
        "before calling the vendor driver.  Executed asynchronously: the "
        "response carries a ``group_job_id`` and per-device job entry the "
        "frontend can poll via ``GET /api/v1/jobs/{job_id}`` and "
        "``GET /api/v1/group-jobs/{id}``.  Pre-state is captured for rollback "
        "— if the device-side change fails, the original access VLAN is "
        "restored automatically.  Requires operator role or higher; "
        "site-scoped users may only target devices in their allowed sites."
    ),
)
def set_port_access_vlan(
    data: PortAccessVlanUpdateRequest,
    current_user: dict = Depends(require_authenticated),
):
    """Schedule an access-VLAN assignment on a single port."""
    try:
        port_validator.validate_interface_name(data.interface)
        port_validator.validate_access_vlan_id(data.vlan_id)
    except ValueError as exc:
        raise ValidationError(str(exc))

    if not device_service.get_device(data.device):
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(current_user, data.device, min_role="operator")

    _require_port_driver_with("set_port_access_vlan", data.device, current_user)

    jobs, group_job_id = port_execution_service.enqueue_set_access_vlan_job(
        interface=data.interface,
        vlan_id=data.vlan_id,
        device=data.device,
        username=current_user["username"],
        retry_base_delay=_RETRY_BASE_DELAY,
    )
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}


@router.patch(
    "/trunk-vlans",
    summary="Set trunk allowed VLANs",
    description=(
        "Modify the trunk allowed-VLAN list on a single interface.  "
        "``mode='replace'`` sets the list to exactly ``vlans``; "
        "``mode='add'`` unions ``vlans`` with the current list; "
        "``mode='remove'`` subtracts ``vlans`` from the current list.  "
        "The port must already be in trunk mode.  Executed asynchronously: "
        "the response carries a ``group_job_id`` and per-device job entry "
        "the frontend can poll via ``GET /api/v1/jobs/{job_id}`` and "
        "``GET /api/v1/group-jobs/{id}``.  Pre-state is captured for rollback "
        "— if the device-side change fails, the original VLAN list is restored "
        "automatically.  Requires operator role or higher; site-scoped users "
        "may only target devices in their allowed sites."
    ),
)
def set_trunk_allowed_vlans(
    data: PortTrunkVlansUpdateRequest,
    current_user: dict = Depends(require_authenticated),
):
    """Schedule a trunk allowed-VLAN update on a single port."""
    try:
        port_validator.validate_interface_name(data.interface)
        port_validator.validate_trunk_vlan_list(list(data.vlans))
    except ValueError as exc:
        raise ValidationError(str(exc))

    if not device_service.get_device(data.device):
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(current_user, data.device, min_role="operator")

    _require_port_driver_with("set_trunk_allowed_vlans", data.device, current_user)

    jobs, group_job_id = port_execution_service.enqueue_set_trunk_allowed_vlans_job(
        interface=data.interface,
        vlans=list(data.vlans),
        mode=data.mode,
        device=data.device,
        username=current_user["username"],
        retry_base_delay=_RETRY_BASE_DELAY,
    )
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}


@router.post(
    "/configure",
    summary="Configure port (composite)",
    description=(
        "Apply one or more port configuration fields in a single driver call.  "
        "All non-``None`` fields are applied atomically on the device.  "
        "Field application order on Huawei VRP: mode → VLAN → description → admin state.  "
        "Pre-state is captured for rollback — on failure the orchestration layer "
        "reconstructs a rollback request covering the changed fields and attempts "
        "to restore their pre-state values.  "
        "Executed asynchronously: the response carries a ``group_job_id`` and "
        "per-device job entry the frontend can poll via ``GET /api/v1/jobs/{job_id}`` "
        "and ``GET /api/v1/group-jobs/{id}``.  "
        "Requires operator role or higher; site-scoped users may only target "
        "devices in their allowed sites."
    ),
)
def configure_port(
    data: PortConfigureRequest,
    current_user: dict = Depends(require_authenticated),
):
    """Schedule a composite port configuration on a single port."""
    from app.models.port import PortConfigRequest

    try:
        port_validator.validate_interface_name(data.interface)
        if data.access_vlan is not None:
            port_validator.validate_access_vlan_id(data.access_vlan)
        if data.allowed_vlans is not None:
            port_validator.validate_trunk_vlan_list(list(data.allowed_vlans))
    except ValueError as exc:
        raise ValidationError(str(exc))

    if not device_service.get_device(data.device):
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(current_user, data.device, min_role="operator")
    _check_device_not_locked(data.device)

    _require_port_driver_with("configure_port", data.device, current_user)

    try:
        config = PortConfigRequest(
            device=data.device,
            interface=data.interface,
            description=data.description,
            admin_enabled=data.admin_enabled,
            mode=data.mode,
            access_vlan=data.access_vlan,
            allowed_vlans=list(data.allowed_vlans) if data.allowed_vlans else None,
            allowed_vlan_operation=data.allowed_vlan_operation,
        )
    except ValueError as exc:
        raise ValidationError(str(exc))

    jobs, group_job_id = port_config_service.configure_port(
        config=config,
        username=current_user["username"],
        retry_base_delay=_RETRY_BASE_DELAY,
    )
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}


@router.post(
    "/shutdown",
    summary="Shutdown port",
    description=(
        "Administratively disable a single interface (``shutdown`` command).  "
        "No-op when the port is already administratively down.  "
        "Pre-state is captured for rollback — if the device-side command fails "
        "and the port was previously up, the orchestration layer calls "
        "``enable_port`` to restore its state.  "
        "Executed asynchronously: the response carries a ``group_job_id`` and "
        "per-device job entry.  "
        "Requires operator role or higher; site-scoped users may only target "
        "devices in their allowed sites."
    ),
)
def shutdown_port(
    data: PortShutdownRequest,
    current_user: dict = Depends(require_authenticated),
):
    """Schedule an administrative shutdown on a single port."""
    try:
        port_validator.validate_interface_name(data.interface)
    except ValueError as exc:
        raise ValidationError(str(exc))

    if not device_service.get_device(data.device):
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(current_user, data.device, min_role="operator")
    _check_device_not_locked(data.device)

    _require_port_driver_with("shutdown_port", data.device, current_user)

    jobs, group_job_id = port_config_service.shutdown_port(
        interface=data.interface,
        device=data.device,
        username=current_user["username"],
        retry_base_delay=_RETRY_BASE_DELAY,
    )
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}


@router.post(
    "/enable",
    summary="Enable port",
    description=(
        "Administratively enable a single interface (``no shutdown`` / "
        "``undo shutdown`` command).  "
        "No-op when the port is already administratively up.  "
        "Pre-state is captured for rollback — if the device-side command fails "
        "and the port was previously down, the orchestration layer calls "
        "``shutdown_port`` to restore its state.  "
        "Executed asynchronously: the response carries a ``group_job_id`` and "
        "per-device job entry.  "
        "Requires operator role or higher; site-scoped users may only target "
        "devices in their allowed sites."
    ),
)
def enable_port(
    data: PortEnableRequest,
    current_user: dict = Depends(require_authenticated),
):
    """Schedule an administrative enable on a single port."""
    try:
        port_validator.validate_interface_name(data.interface)
    except ValueError as exc:
        raise ValidationError(str(exc))

    if not device_service.get_device(data.device):
        raise NotFoundError(f"Device '{data.device}' not found")
    _authz_device(current_user, data.device, min_role="operator")
    _check_device_not_locked(data.device)

    _require_port_driver_with("enable_port", data.device, current_user)

    jobs, group_job_id = port_config_service.enable_port(
        interface=data.interface,
        device=data.device,
        username=current_user["username"],
        retry_base_delay=_RETRY_BASE_DELAY,
    )
    return {"success": True, "group_job_id": group_job_id, "jobs": jobs}
