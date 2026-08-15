import logging
from typing import Optional

from app.db.models import DeviceGroupMemberModel, DeviceGroupModel, DeviceModel, SiteModel
from app.db.session import get_session
from app.schemas.device_group import DeviceGroupRead

logger = logging.getLogger(__name__)


def _to_read(row: DeviceGroupModel, session) -> DeviceGroupRead:
    count = session.query(DeviceGroupMemberModel).filter_by(group_id=row.id).count()
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
    """Create a new device group. site_id is required (step 7.4)."""
    with get_session() as session:
        if session.query(DeviceGroupModel).filter_by(name=name).first():
            raise ValueError(f"Device group '{name}' already exists")
        if not session.query(SiteModel).filter_by(id=site_id).first():
            raise ValueError(f"Site {site_id} not found")
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


def list_groups() -> list[DeviceGroupRead]:
    with get_session() as session:
        rows = session.query(DeviceGroupModel).order_by(DeviceGroupModel.name).all()
        return [_to_read(r, session) for r in rows]


def delete_group(group_id: int) -> bool:
    """Delete a group and all its memberships. Returns False if group not found."""
    with get_session() as session:
        row = session.query(DeviceGroupModel).filter_by(id=group_id).first()
        if not row:
            return False
        session.delete(row)  # cascades to DeviceGroupMemberModel via ORM relationship
    logger.info("Device group deleted: id=%s", group_id)
    return True


def add_member(group_id: int, device_name: str) -> bool:
    """Add a device to a group. Idempotent — returns False if already a member.

    Site invariant (step 7.4): the device's `site_id` must match the group's
    `site_id`. A group with `site_id IS NULL` (legacy) rejects all additions —
    the admin must set its site first.
    """
    with get_session() as session:
        group = session.query(DeviceGroupModel).filter_by(id=group_id).first()
        if not group:
            raise ValueError(f"Device group {group_id} not found")
        device = session.query(DeviceModel).filter_by(name=device_name).first()
        if not device:
            raise ValueError(f"Device '{device_name}' not found")
        if group.site_id is None:
            raise ValueError(
                f"Device group {group_id} has no site assigned — set the group's site before adding members"
            )
        if device.site_id != group.site_id:
            raise ValueError(
                f"Device '{device_name}' belongs to site {device.site_id} but group {group_id} requires site {group.site_id}"
            )
        existing = session.query(DeviceGroupMemberModel).filter_by(
            group_id=group_id, device_name=device_name
        ).first()
        if existing:
            return False
        session.add(DeviceGroupMemberModel(group_id=group_id, device_name=device_name))
    logger.info("Added device '%s' to group %s", device_name, group_id)
    return True


def remove_member(group_id: int, device_name: str) -> bool:
    """Remove a device from a group. Returns False if membership did not exist."""
    with get_session() as session:
        row = session.query(DeviceGroupMemberModel).filter_by(
            group_id=group_id, device_name=device_name
        ).first()
        if not row:
            return False
        session.delete(row)
    logger.info("Removed device '%s' from group %s", device_name, group_id)
    return True


def list_group_devices(group_id: int) -> Optional[list[str]]:
    """Return device names in a group, or None if the group does not exist."""
    with get_session() as session:
        group = session.query(DeviceGroupModel).filter_by(id=group_id).first()
        if not group:
            return None
        rows = (
            session.query(DeviceGroupMemberModel)
            .filter_by(group_id=group_id)
            .order_by(DeviceGroupMemberModel.device_name)
            .all()
        )
        return [r.device_name for r in rows]


def remove_device_from_all_groups(device_name: str) -> int:
    """Remove a device from every group it belongs to. Returns number of memberships removed."""
    with get_session() as session:
        deleted = (
            session.query(DeviceGroupMemberModel)
            .filter_by(device_name=device_name)
            .delete(synchronize_session=False)
        )
    if deleted:
        logger.info("Removed device '%s' from %d group(s) on deletion", device_name, deleted)
    return deleted
