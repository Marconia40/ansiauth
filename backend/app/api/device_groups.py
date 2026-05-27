import logging

from fastapi import APIRouter, Depends

from app.core import authz
from app.core.dependencies import require_role
from app.core.exceptions import NotFoundError, ValidationError
from app.schemas.device_group import DeviceGroupCreate, DeviceGroupMemberCreate
from app.services import audit_service, device_group_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/",
    summary="Create device group",
    description="Create a named group for organizing devices. Group names must be unique. Requires admin role.",
)
def create_group(data: DeviceGroupCreate, current_user: dict = Depends(require_role("admin"))):
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
    description="Return all device groups with their member counts. Requires observer role or higher.",
)
def list_groups(current_user: dict = Depends(require_role("observer"))):
    groups = device_group_service.list_groups()
    allowed_sites = authz.allowed_site_ids_for(current_user)
    if allowed_sites is not None:
        # Restricted caller — only show groups whose site_id is in the allowed set.
        # Legacy groups with site_id IS NULL are admin-only by policy.
        groups = [g for g in groups if g.site_id is not None and g.site_id in allowed_sites]
    return {"success": True, "data": [g.model_dump() for g in groups]}


@router.get(
    "/{group_id}",
    summary="Get device group",
    description="Return a single device group by ID, including its member count. Requires observer role or higher.",
)
def get_group(group_id: int, current_user: dict = Depends(require_role("observer"))):
    group = device_group_service.get_group(group_id)
    if not group:
        raise NotFoundError(f"Device group {group_id} not found")
    allowed_sites = authz.allowed_site_ids_for(current_user)
    if allowed_sites is not None:
        if group.site_id is None or group.site_id not in allowed_sites:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail=f"Device group {group_id} is outside your allowed sites")
    return {"success": True, "data": group.model_dump()}


@router.delete(
    "/{group_id}",
    summary="Delete device group",
    description="Permanently delete a device group and all its memberships. Devices themselves are not affected. Requires admin role.",
)
def delete_group(group_id: int, current_user: dict = Depends(require_role("admin"))):
    deleted = device_group_service.delete_group(group_id)
    if not deleted:
        raise NotFoundError(f"Device group {group_id} not found")
    audit_service.log_action(
        user=current_user["username"],
        action="delete_device_group",
        resource="device_group",
        resource_id=str(group_id),
        details={"group_id": group_id},
    )
    return {"success": True, "data": {"group_id": group_id}}


@router.post(
    "/{group_id}/members",
    summary="Add device to group",
    description="Add a device to a group by device name. Idempotent — adding an existing member returns 200 with added=false. Requires admin role.",
)
def add_member(group_id: int, data: DeviceGroupMemberCreate, current_user: dict = Depends(require_role("admin"))):
    try:
        added = device_group_service.add_member(group_id=group_id, device_name=data.device_name)
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
    return {"success": True, "data": {"group_id": group_id, "device_name": data.device_name, "added": added}}


@router.delete(
    "/{group_id}/members/{device_name}",
    summary="Remove device from group",
    description="Remove a device from a group. Returns 404 if the device is not a member. Requires admin role.",
)
def remove_member(group_id: int, device_name: str, current_user: dict = Depends(require_role("admin"))):
    removed = device_group_service.remove_member(group_id=group_id, device_name=device_name)
    if not removed:
        raise NotFoundError(f"Device '{device_name}' is not a member of group {group_id}")
    audit_service.log_action(
        user=current_user["username"],
        action="remove_device_group_member",
        resource="device_group",
        resource_id=str(group_id),
        details={"group_id": group_id, "device_name": device_name},
    )
    return {"success": True, "data": {"group_id": group_id, "device_name": device_name}}


@router.get(
    "/{group_id}/devices",
    summary="List devices in group",
    description="Return the names of all devices belonging to a group, sorted alphabetically. Requires observer role or higher.",
)
def list_group_devices(group_id: int, current_user: dict = Depends(require_role("observer"))):
    group = device_group_service.get_group(group_id)
    if group is None:
        raise NotFoundError(f"Device group {group_id} not found")
    allowed_sites = authz.allowed_site_ids_for(current_user)
    if allowed_sites is not None:
        if group.site_id is None or group.site_id not in allowed_sites:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail=f"Device group {group_id} is outside your allowed sites")
    devices = device_group_service.list_group_devices(group_id) or []
    return {"success": True, "data": {"group_id": group_id, "devices": devices}}
