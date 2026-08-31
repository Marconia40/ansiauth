import logging

from fastapi import APIRouter, Depends

from app.core.exceptions import DefaultGroupImmutableError, NotFoundError, ValidationError
from app.core.response import ok
from app.core.scope import obtener_scope, require_authenticated, require_scope, visible_or_404
from app.models.device_group import DeviceGroup
from app.models.domain_event import DomainEvent
from app.models.visibility_scope import VisibilityScope
from app.schemas.device_group import DeviceGroupCreate, DeviceGroupRead

logger = logging.getLogger(__name__)
router = APIRouter()


def _to_read(group: DeviceGroup) -> dict:
    from app.composition import device_group_repository, site_repository

    site = site_repository.get(group.site_id) if group.site_id else None
    return DeviceGroupRead(
        id=group.id, name=group.name, description=group.description,
        created_at=group.created_at,
        member_count=device_group_repository.contar_miembros(group.id),
        site_id=group.site_id,
        site_name=site.name if site is not None else None,
    ).model_dump()


def _reject_if_default(group_id: int) -> "DeviceGroup | None":
    """Raise 400 if ``group_id`` is a Site's Default (D7 immutability).

    Returns the fetched group (or None if it doesn't exist) so callers that
    already need a DB round-trip here don't have to fetch it again -- e.g.
    delete_group() reuses this as the ``recurso`` for its audit event.
    """
    from app.composition import device_group_repository

    group = device_group_repository.get(group_id)
    if group is not None and group.es_default:
        raise ValidationError(
            f"Group {group_id} is a Site's Default group and is immutable (D7)"
        )
    return group


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
    current_user: dict = Depends(require_scope("create_group")),
):
    from app.composition import device_group_repository, event_dispatcher, site_repository

    if site_repository.get(data.site_id) is None:
        raise ValidationError(f"Site {data.site_id} not found")
    if device_group_repository.existe(name=data.name, site_id=data.site_id):
        raise ValidationError(
            f"Device group '{data.name}' already exists in site {data.site_id}"
        )
    group = device_group_repository.add(DeviceGroup(
        id=None, name=data.name, description=data.description, site_id=data.site_id,
    ))
    event_dispatcher.despachar([DomainEvent(
        "create_device_group", group, None, current_user["username"],
        {"resource_id": group.id, "name": group.name, "site_id": group.site_id},
    )])
    return ok(_to_read(group))


@router.get(
    "/",
    summary="List device groups",
    description=(
        "Return device groups visible to the caller, scoped through "
        "role_assignments."
    ),
)
def list_groups(
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_group_repository

    groups = device_group_repository.visibles_para_usuario(scope)
    return ok([_to_read(g) for g in groups])


@router.get(
    "/{group_id}",
    summary="Get device group",
    description="Return a single device group by ID, including its member count.",
)
def get_group(
    group_id: int,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_group_repository

    group = device_group_repository.get(group_id)
    if not group:
        raise NotFoundError(f"Device group {group_id} not found")
    visible_or_404(scope, group_id, "device_group", "observer", f"Device group {group_id} not found")
    return ok(_to_read(group))


@router.delete(
    "/{group_id}",
    summary="Delete device group",
    description=(
        "Permanently delete a device group. Any member devices are auto-moved to "
        "the Site's Default group (D19). Default groups cannot be deleted (D7)."
    ),
)
def delete_group(
    group_id: int,
    current_user: dict = Depends(require_scope("delete_group")),
):
    from app.composition import device_group_repository, event_dispatcher, inventory

    group = _reject_if_default(group_id)
    try:
        result = device_group_repository.eliminar_con_auto_move(group_id, current_user, inventory)
    except DefaultGroupImmutableError as exc:
        raise ValidationError(str(exc))
    except ValueError as exc:
        raise ValidationError(str(exc))
    if result is None:
        raise NotFoundError(f"Device group {group_id} not found")
    event_dispatcher.despachar([DomainEvent(
        "delete_device_group", group, None, current_user["username"],
        {"resource_id": group_id, "group_id": group_id, "moved_devices": result.get("moved_devices", [])},
    )])
    return ok(result)


@router.get(
    "/{group_id}/devices",
    summary="List devices in group",
    description="Return the names of all devices belonging to a group, sorted alphabetically.",
)
def list_group_devices(
    group_id: int,
    current_user: dict = Depends(require_authenticated),
    scope: VisibilityScope = Depends(obtener_scope),
):
    from app.composition import device_group_repository

    group = device_group_repository.get(group_id)
    if group is None:
        raise NotFoundError(f"Device group {group_id} not found")
    visible_or_404(scope, group_id, "device_group", "observer", f"Device group {group_id} not found")
    from app.db.models import DeviceModel
    from app.db.session import get_session

    with get_session() as session:
        names = [
            r[0]
            for r in session.query(DeviceModel.name)
            .filter(DeviceModel.device_group_id == group_id)
            .order_by(DeviceModel.name)
            .all()
        ]
    return ok({"group_id": group_id, "devices": names})
