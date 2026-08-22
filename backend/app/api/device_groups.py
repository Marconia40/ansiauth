import logging

from fastapi import APIRouter, Depends, HTTPException

from app.core.exceptions import NotFoundError, ValidationError
from app.core.scope import require_authenticated
from app.db.models import DeviceGroupModel, DeviceModel
from app.db.session import get_session
from app.schemas.device_group import DeviceGroupCreate
from app.services import audit_service, device_group_service
from app.services.device_group_service import DefaultGroupImmutableError
from app.services.effective_role import effective_role

logger = logging.getLogger(__name__)
router = APIRouter()


def _reject_if_default(group_id: int) -> None:
    """Return 400 if ``group_id`` is a Site's Default (D7 immutability)."""
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
        "Create a named group for organizing devices. Group names must be unique "
        "within a site. Requires admin role on the target site."
    ),
)
def create_group(
    data: DeviceGroupCreate,
    current_user: dict = Depends(require_authenticated),
):
    with get_session() as session:
        role = effective_role(session, current_user, "site", data.site_id)
    if not current_user.get("is_system_admin") and role != "admin":
        raise ValidationError(
            f"create_group requires admin on site {data.site_id} (got {role or 'none'})"
        )
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
        "Return device groups visible to the caller, scoped through "
        "role_assignments."
    ),
)
def list_groups(current_user: dict = Depends(require_authenticated)):
    groups = device_group_service.list_groups_for_user(current_user)
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
    with get_session() as session:
        role = effective_role(session, current_user, "device_group", group_id)
    if role is None:
        # Hide existence to non-authorized callers.
        raise NotFoundError(f"Device group {group_id} not found")
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
    _reject_if_default(group_id)
    with get_session() as session:
        role = effective_role(session, current_user, "device_group", group_id)
    if not current_user.get("is_system_admin") and role != "admin":
        raise HTTPException(
            status_code=403,
            detail=f"delete_group requires admin on group {group_id} (got {role or 'none'})",
        )
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
    with get_session() as session:
        role = effective_role(session, current_user, "device_group", group_id)
    if role is None:
        raise NotFoundError(f"Device group {group_id} not found")
    with get_session() as session:
        names = [
            r[0]
            for r in session.query(DeviceModel.name)
            .filter(DeviceModel.device_group_id == group_id)
            .order_by(DeviceModel.name)
            .all()
        ]
    return {"success": True, "data": {"group_id": group_id, "devices": names}}
