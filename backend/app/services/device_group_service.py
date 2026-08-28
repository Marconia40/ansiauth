import logging
from typing import Optional

from app.core.config import settings
from app.db.models import (
    DeviceGroupMemberModel,
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
    # MSP: Phase 3 — count members via the authoritative FK when the flag is
    # on so add_member/remove_member no-longer-required paths cannot skew the
    # visible member count. Legacy junction still counts under flag-off.
    if settings.MSP_STRICT_HIERARCHY:
        count = session.query(DeviceModel).filter_by(device_group_id=row.id).count()
    else:
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


def list_groups_for_user(user: dict) -> list[DeviceGroupRead]:
    """MSP: Phase 3 — return groups visible to the caller via role_assignments.

    A caller sees a group when they hold *any* grant on the group's site
    (site-wide grants imply visibility of every group inside the site) or a
    group-specific grant on that group.
    """
    with get_session() as session:
        q = session.query(DeviceGroupModel).filter(DeviceGroupModel.site_id.isnot(None))
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
            .filter(DeviceGroupModel.name == new_name, DeviceGroupModel.id != group_id)
            .first()
        )
        if conflict is not None:
            raise ValueError(f"Device group '{new_name}' already exists")
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
        if site_id is None:
            # Legacy null-site group: no Default to move devices to; refuse.
            raise ValueError(
                f"Group {group_id} has no site assigned — cannot auto-move members"
            )
        default_group_id = (
            session.query(SiteModel.default_group_id)
            .filter(SiteModel.id == site_id)
            .scalar()
        )
        if default_group_id is None:
            raise ValueError(
                f"Site {site_id} has no Default group; cannot auto-move members"
            )
        # Names of every device currently in this group via either the M2M
        # junction OR the direct FK — both populated post-Phase-2.
        members: set[str] = set()
        members.update(
            r[0]
            for r in session.query(DeviceGroupMemberModel.device_name)
            .filter_by(group_id=group_id)
            .all()
        )
        members.update(
            r[0]
            for r in session.query(DeviceModel.name)
            .filter_by(device_group_id=group_id)
            .all()
        )
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


def add_member(
    group_id: int,
    device_name: str,
    *,
    actor: Optional[dict] = None,
) -> bool:
    """Add a device to a group. Idempotent — returns False if already a member.

    Site invariant (step 7.4): the device's `site_id` must match the group's
    `site_id`. A group with `site_id IS NULL` (legacy) rejects all additions —
    the admin must set its site first.

    MSP: Phase 3 — when the flag is on this delegates to ``Inventory.move`` so
    the device's authoritative FK (``devices.device_group_id``) stays in sync.
    The legacy junction row is *also* written so flag-off callers keep the
    same view of the world.
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
        # Site invariant check uses the *authoritative* site under the MSP
        # flag (derived through device_group), and the legacy row otherwise.
        current_site_id = (
            device.device_group.site_id
            if settings.MSP_STRICT_HIERARCHY and device.device_group is not None
            else device.site_id
        )
        if current_site_id != group.site_id:
            raise ValueError(
                f"Device '{device_name}' belongs to site {current_site_id} "
                f"but group {group_id} requires site {group.site_id}"
            )
        existing = session.query(DeviceGroupMemberModel).filter_by(
            group_id=group_id, device_name=device_name
        ).first()
        added_now = existing is None
        if added_now:
            session.add(DeviceGroupMemberModel(group_id=group_id, device_name=device_name))
        already_here_msp = (
            settings.MSP_STRICT_HIERARCHY and device.device_group_id == group_id
        )
    # Keep the direct FK in sync under the MSP flag. Skip when it already
    # points at this group (idempotent short-circuit).
    if settings.MSP_STRICT_HIERARCHY and not already_here_msp:
        from app.services.inventory_service import Inventory
        Inventory().move(
            device_name,
            target_group_id=group_id,
            actor=actor or {"username": "system", "id": None, "is_system_admin": True},
            reason="add_member",
            enforce_authz=False,
        )
    logger.info("Added device '%s' to group %s (junction=%s)", device_name, group_id, added_now)
    return added_now


def remove_member(
    group_id: int,
    device_name: str,
    *,
    actor: Optional[dict] = None,
) -> bool:
    """Remove a device from a group.

    Per D8 the *action* of "no longer in this group" is device-scoped: when
    the MSP flag is on, this delegates to ``Inventory.move`` targeting the
    site's Default group so the device is never orphaned. The legacy junction
    row is deleted regardless so flag-off callers see a consistent result.

    Returns False when there was nothing to remove.
    """
    from app.services.inventory_service import Inventory

    junction_removed = False
    fk_needed_move = False
    site_id: Optional[int] = None
    with get_session() as session:
        junction_row = session.query(DeviceGroupMemberModel).filter_by(
            group_id=group_id, device_name=device_name
        ).first()
        if junction_row is not None:
            session.delete(junction_row)
            junction_removed = True
        # Under the MSP flag, also handle the direct FK — this is the D8 hook.
        if settings.MSP_STRICT_HIERARCHY:
            device = session.query(DeviceModel).filter_by(name=device_name).first()
            if device is not None and device.device_group_id == group_id:
                grp = session.query(DeviceGroupModel).filter_by(id=group_id).first()
                if grp is not None and grp.site_id is not None:
                    site_id = grp.site_id
                    fk_needed_move = True
    if fk_needed_move:
        # Resolve default group and move — device-level action, per D8.
        with get_session() as session:
            default_group_id = (
                session.query(SiteModel.default_group_id)
                .filter(SiteModel.id == site_id)
                .scalar()
            )
        if default_group_id is None:
            raise ValueError(
                f"Site {site_id} has no Default group; cannot remove device from group"
            )
        Inventory().move(
            device_name,
            target_group_id=default_group_id,
            actor=actor or {"username": "system", "id": None, "is_system_admin": True},
            reason="remove_member_via_group_endpoint",
            enforce_authz=False,
        )
        return True
    return junction_removed


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
