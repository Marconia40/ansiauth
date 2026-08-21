import logging

from fastapi import APIRouter, Depends

from app.core import authz
from app.core.config import settings
from app.core.dependencies import require_role
from app.core.exceptions import NotFoundError, ValidationError
from app.core.scope import require_authenticated, require_scope
from app.schemas.device import DeviceCreate, DeviceMove, DevicePublic, DeviceUpdate
from app.services import audit_service, device_service
from app.services.inventory_service import Inventory

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
    if settings.MSP_STRICT_HIERARCHY:
        devices = Inventory().list(current_user, site_id=site_id, device_group_id=device_group_id)
        return {"success": True, "data": [_to_public(d) for d in devices]}
    # Flag-off: legacy path preserved verbatim for T3.5 snapshot-diff parity.
    devices = device_service.get_devices()
    allowed = authz.allowed_device_names_for(current_user)
    if allowed is not None:
        devices = [d for d in devices if d.name in allowed]
    if site_id is not None:
        devices = [d for d in devices if d.site_id == site_id]
    if device_group_id is not None:
        devices = [d for d in devices if d.device_group_id == device_group_id]
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
    # ESC-T3.4b (devices.py:50) + phase-3 §2: flag-on uses per-scope authz;
    # flag-off keeps the legacy check to protect the snapshot-diff invariant.
    if settings.MSP_STRICT_HIERARCHY:
        _ = Depends  # placeholder to keep the import symmetric
        # For non-system-admins the require_scope dep runs *before* this
        # function per the endpoint declaration — but list/get can't easily
        # hoist require_scope into Depends because the flag might be off.
        # Instead we call effective_role here directly.
        from app.services.effective_role import effective_role
        from app.db.session import get_session
        with get_session() as session:
            role = effective_role(session, current_user, "device", name)
        if role is None:
            raise NotFoundError(f"Device '{name}' not found")
    else:
        authz.ensure_device_allowed(current_user, name)
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
def create_device(
    data: DeviceCreate,
    current_user: dict = Depends(require_authenticated),
):
    # Under MSP-strict, the site_id in the body (or the target group's site)
    # is the authorization target: require admin there. Under flag-off we
    # keep the legacy require_role("admin") gate.
    if settings.MSP_STRICT_HIERARCHY:
        # The scope dep already reads the body — but Depends can't be swapped
        # at runtime by a flag. Run effective_role inline for the flag-on path.
        from app.services.effective_role import effective_role
        from app.db.session import get_session
        with get_session() as session:
            role = effective_role(session, current_user, "site", data.site_id) if data.site_id else None
        if not current_user.get("is_system_admin") and role != "admin":
            raise ValidationError(
                f"register_device requires admin on site {data.site_id} (got {role or 'none'})"
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
                device_group_id=None,  # DeviceCreate has no group field pre-Phase-4
                actor=current_user,
            )
        except ValueError as e:
            raise ValidationError(str(e))
        return {"success": True, "data": _to_public(device)}

    # Legacy path — unchanged from Phase 2.
    _ = require_role("admin")  # documentation-only: legacy require_role gate
    if current_user["role"] not in {"admin", "super-admin"}:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Insufficient permissions")
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
def update_device(
    name: str,
    data: DeviceUpdate,
    current_user: dict = Depends(require_authenticated),
):
    provided = data.model_dump(exclude_unset=True)
    if not provided:
        raise ValidationError("No fields provided for update")
    if settings.MSP_STRICT_HIERARCHY:
        # Reject site_id in body: per MSP, site is derived from device_group.
        # Callers change site via POST /devices/{name}/move.
        if "site_id" in provided:
            raise ValidationError(
                "site_id cannot be set via PUT /devices/{name} when MSP hierarchy "
                "is enforced; use POST /devices/{name}/move instead"
            )
        from app.services.effective_role import effective_role
        from app.db.session import get_session
        with get_session() as session:
            role = effective_role(session, current_user, "device", name)
        if not current_user.get("is_system_admin") and role != "admin":
            raise ValidationError(
                f"edit_device requires admin on device '{name}' (got {role or 'none'})"
            )
    else:
        if current_user["role"] not in {"admin", "super-admin"}:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
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
        "Requires operator role or higher."
    ),
)
def save_device_config(
    name: str,
    current_user: dict = Depends(require_authenticated),
):
    device = device_service.get_device(name)
    if not device:
        raise NotFoundError(f"Device '{name}' not found")
    # ESC-T3.4b (devices.py:147): swap legacy ensure_device_allowed → require_scope.
    if settings.MSP_STRICT_HIERARCHY:
        from app.services.effective_role import effective_role
        from app.db.session import get_session
        _ROLE_LEVEL = {"observer": 1, "operator": 2, "admin": 3, "super-admin": 99}
        with get_session() as session:
            role = effective_role(session, current_user, "device", name)
        if _ROLE_LEVEL.get(role or "", 0) < _ROLE_LEVEL["operator"]:
            from fastapi import HTTPException
            raise HTTPException(
                status_code=403,
                detail=(
                    f"write_device_config requires operator on device '{name}' "
                    f"(got {role or 'none'})"
                ),
            )
    else:
        if current_user["role"] not in {"operator", "admin", "super-admin"}:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        authz.ensure_device_allowed(current_user, name)
    from app.services.vlan_execution_service import enqueue_save_job
    entry = enqueue_save_job(name, current_user["username"])
    return {"success": True, "data": entry}


@router.delete(
    "/{name}",
    summary="Delete device",
    description="Permanently remove a device from the inventory. Requires admin role.",
)
def delete_device(name: str, current_user: dict = Depends(require_authenticated)):
    if settings.MSP_STRICT_HIERARCHY:
        from app.services.effective_role import effective_role
        from app.db.session import get_session
        with get_session() as session:
            role = effective_role(session, current_user, "device", name)
        if not current_user.get("is_system_admin") and role != "admin":
            from fastapi import HTTPException
            raise HTTPException(
                status_code=403,
                detail=f"delete_device requires admin on device '{name}' (got {role or 'none'})",
            )
        Inventory().deregister(name, actor=current_user)
        return {"success": True, "data": {"name": name}}
    if current_user["role"] not in {"admin", "super-admin"}:
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Insufficient permissions")
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
