import logging
from typing import Optional

from app.db.models import (
    DeviceGroupModel,
    DeviceModel,
    RoleAssignmentModel,
    SiteModel,
)
from app.db.session import get_session
from app.schemas.device_group import DeviceGroupRead

logger = logging.getLogger(__name__)


class DefaultGroupImmutableError(ValueError):
    """Raised by rename/delete when the target group is a Site's Default (D7)."""


class GroupHasDevicesError(ValueError):
    """Raised when a group deletion would move devices but the caller opted
    into strict-empty semantics. Not used by the normal delete path — devices
    are auto-moved to the Site's Default group by ``delete_group`` (D19)."""


def _to_read(row: DeviceGroupModel, session) -> DeviceGroupRead:
    count = session.query(DeviceModel).filter_by(device_group_id=row.id).count()
    site_name = row.site.name if row.site is not None else None
    return DeviceGroupRead(
        id=row.id,
        name=row.name,
        description=row.description,
        created_at=row.created_at,
        member_count=count,
        site_id=row.site_id,
        site_name=site_name,
    )


def create_group(name: str, description: Optional[str], site_id: int) -> DeviceGroupRead:
    """Create a new device group in the given site.

    Per D6 the name must be unique inside the site; the ``UNIQUE(site_id, name)``
    constraint enforces this at the DB layer.
    """
    with get_session() as session:
        if not session.query(SiteModel).filter_by(id=site_id).first():
            raise ValueError(f"Site {site_id} not found")
        conflict = (
            session.query(DeviceGroupModel)
            .filter_by(name=name, site_id=site_id)
            .first()
        )
        if conflict is not None:
            raise ValueError(
                f"Device group '{name}' already exists in site {site_id}"
            )
        row = DeviceGroupModel(name=name, description=description, site_id=site_id)
        session.add(row)
        session.flush()
        result = _to_read(row, session)
    logger.info("Device group created: name=%s site_id=%s", name, site_id)
    return result


def get_group(group_id: int) -> Optional[DeviceGroupRead]:
    with get_session() as session:
        row = session.query(DeviceGroupModel).filter_by(id=group_id).first()
        return _to_read(row, session) if row else None


def list_groups_for_user(user: dict) -> list[DeviceGroupRead]:
    """Return groups visible to the caller via role_assignments.

    A caller sees a group when they hold *any* grant on the group's site
    (site-wide grants imply visibility of every group inside the site) or a
    group-specific grant on that group.
    """
    with get_session() as session:
        q = session.query(DeviceGroupModel)
        if not user.get("is_system_admin"):
            user_id = user.get("id")
            if user_id is None:
                return []
            grants = (
                session.query(
                    RoleAssignmentModel.site_id,
                    RoleAssignmentModel.device_group_id,
                )
                .filter(RoleAssignmentModel.user_id == user_id)
                .all()
            )
            visible_site_ids = {sid for (sid, gid) in grants if gid is None}
            visible_group_ids = {gid for (_sid, gid) in grants if gid is not None}
            if not visible_site_ids and not visible_group_ids:
                return []
            from sqlalchemy import or_
            q = q.filter(
                or_(
                    DeviceGroupModel.site_id.in_(visible_site_ids) if visible_site_ids else False,
                    DeviceGroupModel.id.in_(visible_group_ids) if visible_group_ids else False,
                )
            )
            # D14: Base-Infrastructure groups are hidden from non system-admins.
            from app.services.site_service import BASE_INFRA_SITE_KIND
            hidden_site_ids = {
                sid for (sid,) in (
                    session.query(SiteModel.id)
                    .filter(SiteModel.kind == BASE_INFRA_SITE_KIND)
                    .all()
                )
            }
            if hidden_site_ids:
                q = q.filter(~DeviceGroupModel.site_id.in_(hidden_site_ids))
        rows = q.order_by(DeviceGroupModel.name).all()
        return [_to_read(r, session) for r in rows]


def list_site_groups(site_id: int) -> Optional[list[DeviceGroupRead]]:
    """Return every group in the given site, or None if the site does not exist."""
    with get_session() as session:
        if not session.query(SiteModel.id).filter_by(id=site_id).first():
            return None
        rows = (
            session.query(DeviceGroupModel)
            .filter_by(site_id=site_id)
            .order_by(DeviceGroupModel.name)
            .all()
        )
        return [_to_read(r, session) for r in rows]


def rename_group(group_id: int, new_name: str) -> Optional[DeviceGroupRead]:
    """Rename a group. Raises DefaultGroupImmutableError (D7) for a Default group.

    Returns None if the group does not exist; the new schema is validated by
    the pydantic layer, not here.
    """
    with get_session() as session:
        row = session.query(DeviceGroupModel).filter_by(id=group_id).first()
        if row is None:
            return None
        if row.is_default:
            raise DefaultGroupImmutableError(
                f"Group {group_id} is a Site's Default group and cannot be renamed (D7)"
            )
        conflict = (
            session.query(DeviceGroupModel)
            .filter(
                DeviceGroupModel.name == new_name,
                DeviceGroupModel.site_id == row.site_id,
                DeviceGroupModel.id != group_id,
            )
            .first()
        )
        if conflict is not None:
            raise ValueError(
                f"Device group '{new_name}' already exists in site {row.site_id}"
            )
        row.name = new_name
        session.flush()
        return _to_read(row, session)


def delete_group(group_id: int, *, actor: Optional[dict] = None) -> Optional[dict]:
    """Delete a group.

    Returns None if the group does not exist. Raises
    :class:`DefaultGroupImmutableError` (D7) if the target is a Site's
    Default group. Otherwise auto-moves every member device to the Site's
    Default group (D19) via the Inventory service, then deletes the group.

    Returns a dict describing the outcome:
        ``{"deleted_group_id": <id>, "moved_devices": [<name>, ...]}``.
    """
    # Lazy import breaks the ``device_group_service → inventory_service``
    # circular import that would otherwise happen at load time.
    from app.services.inventory_service import Inventory

    with get_session() as session:
        row = session.query(DeviceGroupModel).filter_by(id=group_id).first()
        if row is None:
            return None
        if row.is_default:
            raise DefaultGroupImmutableError(
                f"Group {group_id} is a Site's Default group and cannot be deleted (D7)"
            )
        site_id = row.site_id
        default_group_id = (
            session.query(SiteModel.default_group_id)
            .filter(SiteModel.id == site_id)
            .scalar()
        )
        if default_group_id is None:
            raise ValueError(
                f"Site {site_id} has no Default group; cannot auto-move members"
            )
        members: set[str] = {
            r[0]
            for r in session.query(DeviceModel.name)
            .filter_by(device_group_id=group_id)
            .all()
        }
    # Move each device via Inventory outside the session context to keep the
    # audit rows independent transactions; the group deletion happens last.
    moved: list[str] = []
    if members:
        inv = Inventory()
        for name in sorted(members):
            inv.move(
                name,
                target_group_id=default_group_id,
                actor=actor or {"username": "system", "id": None, "is_system_admin": True},
                reason="auto_move_on_group_delete",
                enforce_authz=False,
            )
            moved.append(name)
    with get_session() as session:
        row = session.query(DeviceGroupModel).filter_by(id=group_id).first()
        if row is None:
            # Someone else already deleted it — that's fine.
            return {"deleted_group_id": group_id, "moved_devices": moved}
        session.delete(row)
    logger.info(
        "Device group deleted: id=%s moved_devices=%d", group_id, len(moved)
    )
    return {"deleted_group_id": group_id, "moved_devices": moved}
