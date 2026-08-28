import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core import authz
from app.core.config import settings
from app.core.dependencies import require_role
from app.core.exceptions import NotFoundError, ValidationError
from app.core.scope import require_authenticated
from app.schemas.device_group import DeviceGroupCreate, DeviceGroupMemberCreate
from app.services import audit_service, device_group_service
from app.services.device_group_service import DefaultGroupImmutableError

logger = logging.getLogger(__name__)
router = APIRouter()


def _reject_if_default(group_id: int) -> None:
    """Return 400 if ``group_id`` is a Site's Default (D7 immutability)."""
    from app.db.session import get_session
    from app.db.models import DeviceGroupModel
    with get_session() as session:
        row = (
            session.query(DeviceGroupModel.is_default)
            .filter(DeviceGroupModel.id == group_id)
            .first()
        )
    if row is not None and bool(row[0]):
        raise ValidationError(
            f"Group {group_id} is a Site's Default group and is immutable (D7)"
        )


@router.post(
    "/",
    summary="Create device group",
    description=(
        "Create a named group for organizing devices. Group names must be unique. "
        "Requires admin role on the target site."
    ),
)
def create_group(
    data: DeviceGroupCreate,
    current_user: dict = Depends(require_authenticated),
):
    if settings.MSP_STRICT_HIERARCHY:
        from app.services.effective_role import effective_role
        from app.db.session import get_session
        with get_session() as session:
            role = effective_role(session, current_user, "site", data.site_id)
        if not current_user.get("is_system_admin") and role != "admin":
            raise ValidationError(
                f"create_group requires admin on site {data.site_id} (got {role or 'none'})"
            )
    else:
        if current_user["role"] not in {"admin", "super-admin"}:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        group = device_group_service.create_group(
            name=data.name,
            description=data.description,
            site_id=data.site_id,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    audit_service.log_action(
        user=current_user["username"],
        action="create_device_group",
        resource="device_group",
        resource_id=str(group.id),
        details={"name": group.name, "site_id": group.site_id},
    )
    return {"success": True, "data": group.model_dump()}


@router.get(
    "/",
    summary="List device groups",
    description=(
        "Return device groups visible to the caller. Under MSP-strict this uses "
        "role_assignments; otherwise it falls back to the legacy allowed_sites scope."
    ),
)
def list_groups(current_user: dict = Depends(require_authenticated)):
    if settings.MSP_STRICT_HIERARCHY:
        groups = device_group_service.list_groups_for_user(current_user)
    else:
        groups = device_group_service.list_groups()
        allowed_sites = authz.allowed_site_ids_for(current_user)
        if allowed_sites is not None:
            groups = [
                g for g in groups
                if g.site_id is not None and g.site_id in allowed_sites
            ]
    return {"success": True, "data": [g.model_dump() for g in groups]}


@router.get(
    "/{group_id}",
    summary="Get device group",
    description="Return a single device group by ID, including its member count.",
)
def get_group(group_id: int, current_user: dict = Depends(require_authenticated)):
    group = device_group_service.get_group(group_id)
    if not group:
        raise NotFoundError(f"Device group {group_id} not found")
    if settings.MSP_STRICT_HIERARCHY:
        from app.services.effective_role import effective_role
        from app.db.session import get_session
        with get_session() as session:
            role = effective_role(session, current_user, "device_group", group_id)
        if role is None:
            # Hide existence to non-authorized callers.
            raise NotFoundError(f"Device group {group_id} not found")
    else:
        allowed_sites = authz.allowed_site_ids_for(current_user)
        if allowed_sites is not None:
            if group.site_id is None or group.site_id not in allowed_sites:
                raise HTTPException(
                    status_code=403,
                    detail=f"Device group {group_id} is outside your allowed sites",
                )
    return {"success": True, "data": group.model_dump()}


@router.delete(
    "/{group_id}",
    summary="Delete device group",
    description=(
        "Permanently delete a device group. Any member devices are auto-moved to "
        "the Site's Default group (D19). Default groups cannot be deleted (D7)."
    ),
)
def delete_group(group_id: int, current_user: dict = Depends(require_authenticated)):
    if settings.MSP_STRICT_HIERARCHY:
        _reject_if_default(group_id)
        from app.services.effective_role import effective_role
        from app.db.session import get_session
        with get_session() as session:
            role = effective_role(session, current_user, "device_group", group_id)
        if not current_user.get("is_system_admin") and role != "admin":
            raise HTTPException(
                status_code=403,
                detail=f"delete_group requires admin on group {group_id} (got {role or 'none'})",
            )
    else:
        if current_user["role"] not in {"admin", "super-admin"}:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        result = device_group_service.delete_group(group_id, actor=current_user)
    except DefaultGroupImmutableError as exc:
        raise ValidationError(str(exc))
    except ValueError as exc:
        raise ValidationError(str(exc))
    if result is None:
        raise NotFoundError(f"Device group {group_id} not found")
    audit_service.log_action(
        user=current_user["username"],
        action="delete_device_group",
        resource="device_group",
        resource_id=str(group_id),
        details={
            "group_id": group_id,
            "moved_devices": result.get("moved_devices", []),
        },
    )
    return {"success": True, "data": result}


@router.post(
    "/{group_id}/members",
    summary="Add device to group",
    description=(
        "Add a device to a group. Idempotent — an existing member returns 200 "
        "with added=false. Under MSP-strict, this delegates to Inventory.move so "
        "the device's authoritative group FK is updated too. Requires admin role."
    ),
)
def add_member(
    group_id: int,
    data: DeviceGroupMemberCreate,
    current_user: dict = Depends(require_authenticated),
):
    if settings.MSP_STRICT_HIERARCHY:
        from app.services.effective_role import effective_role
        from app.db.session import get_session
        with get_session() as session:
            role = effective_role(session, current_user, "device_group", group_id)
        if not current_user.get("is_system_admin") and role != "admin":
            raise HTTPException(
                status_code=403,
                detail=f"edit_group requires admin on group {group_id} (got {role or 'none'})",
            )
    else:
        if current_user["role"] not in {"admin", "super-admin"}:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        added = device_group_service.add_member(
            group_id=group_id, device_name=data.device_name, actor=current_user,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    if added:
        audit_service.log_action(
            user=current_user["username"],
            action="add_device_group_member",
            resource="device_group",
            resource_id=str(group_id),
            details={"group_id": group_id, "device_name": data.device_name},
        )
    return {
        "success": True,
        "data": {
            "group_id": group_id,
            "device_name": data.device_name,
            "added": added,
        },
    }


@router.delete(
    "/{group_id}/members/{device_name}",
    summary="Remove device from group (legacy compat)",
    description=(
        "**Deprecated shape** — kept for backward compatibility. Per D8, "
        "'remove from group' is a device-level action; prefer "
        "`POST /devices/{name}/move` with `device_group_id: null` to move a "
        "device back to its site's Default group. Under MSP-strict this "
        "endpoint delegates to that exact operation so both surfaces converge."
    ),
    deprecated=True,
)
def remove_member(
    group_id: int,
    device_name: str,
    current_user: dict = Depends(require_authenticated),
):
    if settings.MSP_STRICT_HIERARCHY:
        # Authz mirrors the device-level move: caller needs operator on the
        # device (same-site) because the D8 semantics land the device in its
        # site's Default group — never a cross-site move.
        from app.services.effective_role import effective_role
        from app.db.session import get_session
        _ROLE_LEVEL = {"observer": 1, "operator": 2, "admin": 3, "super-admin": 99}
        with get_session() as session:
            role = effective_role(session, current_user, "device", device_name)
        if _ROLE_LEVEL.get(role or "", 0) < _ROLE_LEVEL["operator"]:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"remove_member requires operator on device '{device_name}' "
                    f"(got {role or 'none'})"
                ),
            )
    else:
        if current_user["role"] not in {"admin", "super-admin"}:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        removed = device_group_service.remove_member(
            group_id=group_id, device_name=device_name, actor=current_user,
        )
    except ValueError as e:
        raise ValidationError(str(e))
    if not removed:
        raise NotFoundError(
            f"Device '{device_name}' is not a member of group {group_id}"
        )
    audit_service.log_action(
        user=current_user["username"],
        action="remove_device_group_member",
        resource="device_group",
        resource_id=str(group_id),
        details={"group_id": group_id, "device_name": device_name},
    )
    return {
        "success": True,
        "data": {"group_id": group_id, "device_name": device_name},
    }


@router.get(
    "/{group_id}/devices",
    summary="List devices in group",
    description="Return the names of all devices belonging to a group, sorted alphabetically.",
)
def list_group_devices(
    group_id: int,
    current_user: dict = Depends(require_authenticated),
):
    group = device_group_service.get_group(group_id)
    if group is None:
        raise NotFoundError(f"Device group {group_id} not found")
    if settings.MSP_STRICT_HIERARCHY:
        from app.services.effective_role import effective_role
        from app.db.session import get_session
        with get_session() as session:
            role = effective_role(session, current_user, "device_group", group_id)
        if role is None:
            raise NotFoundError(f"Device group {group_id} not found")
        # Prefer the authoritative FK when it's populated; fall back to the
        # legacy junction so callers before Phase-2 backfill still work.
        from app.db.models import DeviceModel
        with get_session() as session:
            names_via_fk = [
                r[0]
                for r in session.query(DeviceModel.name)
                .filter(DeviceModel.device_group_id == group_id)
                .order_by(DeviceModel.name)
                .all()
            ]
        if names_via_fk:
            return {"success": True, "data": {"group_id": group_id, "devices": names_via_fk}}
    else:
        allowed_sites = authz.allowed_site_ids_for(current_user)
        if allowed_sites is not None:
            if group.site_id is None or group.site_id not in allowed_sites:
                raise HTTPException(
                    status_code=403,
                    detail=f"Device group {group_id} is outside your allowed sites",
                )
    devices = device_group_service.list_group_devices(group_id) or []
    return {"success": True, "data": {"group_id": group_id, "devices": devices}}
