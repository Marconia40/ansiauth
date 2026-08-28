import logging
from typing import Optional

from app.core.config import settings
from app.db.models import (
    DeviceGroupModel,
    DeviceModel,
    RoleAssignmentModel,
    SiteModel,
)
from app.db.session import get_session
from app.schemas.site import SiteRead

logger = logging.getLogger(__name__)

# MSP: Phase 1 — constants for the Base-Infrastructure Site and every Site's
# Default Group. Reused by Phase 2 backfill; keep in sync with the migration.
BASE_INFRA_SITE_NAME = "Base Infrastructure"
BASE_INFRA_SITE_KIND = "BASE_INFRASTRUCTURE"
REGULAR_SITE_KIND = "REGULAR"
DEFAULT_GROUP_NAME = "Default"


class SiteHasDevicesError(Exception):
    """Raised when attempting to delete a site that still contains devices."""

    def __init__(self, site_id: int, device_count: int):
        super().__init__(
            f"Site {site_id} still contains {device_count} device(s) — reassign them before deletion"
        )
        self.site_id = site_id
        self.device_count = device_count


def _to_read(row: SiteModel, session) -> SiteRead:
    count = session.query(DeviceModel).filter_by(site_id=row.id).count()
    return SiteRead(
        id=row.id,
        name=row.name,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
        device_count=count,
    )


def create_site(name: str, description: Optional[str] = None) -> SiteRead:
    """Create a new site plus its Default DeviceGroup, atomically.

    Per phase-3 §8 (steps 2-4 of MSP §15): INSERT site → INSERT default group
    → UPDATE ``sites.default_group_id`` all happen inside one transaction so
    a site can never be visible without its Default group.
    """
    with get_session() as session:
        if session.query(SiteModel).filter_by(name=name).first():
            raise ValueError(f"Site '{name}' already exists")
        row = SiteModel(name=name, description=description)
        session.add(row)
        session.flush()  # populate row.id before referencing it below
        # A regular site's Default group is created here and cannot be renamed
        # or deleted (see device_group_service enforcement of D7). Under M3
        # ``UNIQUE(site_id, name)`` allows every site to have a plain
        # ``Default``; the per-site-suffix fallback in
        # ``_default_group_name_for_site`` remains for edge cases where a
        # user pre-created a "Default" and never named the site's own default.
        default_group = DeviceGroupModel(
            name=_default_group_name_for_site(session, row.id),
            site_id=row.id,
            is_default=True,
            description=f"Default group for site '{name}'.",
        )
        session.add(default_group)
        session.flush()
        row.default_group_id = default_group.id
        session.flush()
        default_group_id_snapshot = default_group.id
        result = _to_read(row, session)
    logger.info(
        "Site created: name=%s default_group_id=%s", name, default_group_id_snapshot,
    )
    return result


def _default_group_name_for_site(session, site_id: int) -> str:
    """Return the per-site Default group name.

    MSP: Phase 4 (M3) — the global ``UNIQUE(device_groups.name)`` is gone;
    ``UNIQUE(site_id, name)`` takes its place, so every site can carry a
    plain ``Default`` group without colliding. The per-site-suffix fallback
    is retained for the edge case where an operator hand-created a group
    literally named ``Default`` in *this* site before the auto-provisioning
    ran (which would collide with the new insert).
    """
    base = DEFAULT_GROUP_NAME
    existing = (
        session.query(DeviceGroupModel)
        .filter_by(name=base, site_id=site_id)
        .first()
    )
    if existing is None:
        return base
    return f"{DEFAULT_GROUP_NAME} (site {site_id})"


def get_site(site_id: int) -> Optional[SiteRead]:
    with get_session() as session:
        row = session.query(SiteModel).filter_by(id=site_id).first()
        return _to_read(row, session) if row else None


def list_sites() -> list[SiteRead]:
    with get_session() as session:
        rows = session.query(SiteModel).order_by(SiteModel.name).all()
        return [_to_read(r, session) for r in rows]


def list_sites_for_user(user: dict) -> list[SiteRead]:
    """MSP: Phase 3 — return sites visible to the caller via role_assignments.

    Rules:
      * ``is_system_admin=True`` → every site (including Base-Infrastructure).
      * Otherwise: every site the caller has at least one grant on. The
        Base-Infrastructure site is hidden from non system-admins (D14) unless
        they were explicitly granted a role on it (never happens by default —
        only the migration Phase 2 grants system-admins into it).
    """
    with get_session() as session:
        q = session.query(SiteModel)
        if not user.get("is_system_admin"):
            user_id = user.get("id")
            if user_id is None:
                return []
            granted_site_ids = {
                sid for (sid,) in (
                    session.query(RoleAssignmentModel.site_id)
                    .filter(RoleAssignmentModel.user_id == user_id)
                    .distinct()
                    .all()
                )
            }
            if not granted_site_ids:
                return []
            q = q.filter(SiteModel.id.in_(granted_site_ids))
            # D14: hide Base-Infrastructure from anyone who is not a
            # system-admin — even if a stray grant landed on it.
            q = q.filter(SiteModel.kind != BASE_INFRA_SITE_KIND)
        rows = q.order_by(SiteModel.name).all()
        return [_to_read(r, session) for r in rows]


def update_site(
    site_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> Optional[SiteRead]:
    """Update a site. Returns None if not found.

    A `description` of None is treated as "no change" — clearing the description
    is intentionally not supported through this minimal CRUD surface.
    """
    with get_session() as session:
        row = session.query(SiteModel).filter_by(id=site_id).first()
        if not row:
            return None
        if name is not None and name != row.name:
            conflict = session.query(SiteModel).filter(
                SiteModel.name == name, SiteModel.id != site_id
            ).first()
            if conflict:
                raise ValueError(f"Site '{name}' already exists")
            row.name = name
        if description is not None:
            row.description = description
        session.flush()
        result = _to_read(row, session)
    logger.info("Site updated: id=%s", site_id)
    return result


def delete_site(site_id: int) -> bool:
    """Delete a site. Raises SiteHasDevicesError if the site still owns devices.

    Returns False if the site does not exist; True on success.
    """
    with get_session() as session:
        row = session.query(SiteModel).filter_by(id=site_id).first()
        if not row:
            return False
        # MSP: Phase 3 — the Base-Infrastructure site is system-managed and
        # can never be deleted; the API layer surfaces this as HTTP 400.
        if row.kind == BASE_INFRA_SITE_KIND:
            raise ValueError(
                "The Base-Infrastructure site is system-managed and cannot be deleted"
            )
        # Legacy devices FK on sites.id (SET NULL) and the MSP-authoritative
        # path via device_groups.site_id both count as "devices in this site"
        # for the purposes of the delete guard.
        legacy_count = session.query(DeviceModel).filter_by(site_id=site_id).count()
        msp_count = (
            session.query(DeviceModel)
            .join(DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id)
            .filter(DeviceGroupModel.site_id == site_id)
            .count()
        )
        device_count = max(legacy_count, msp_count)
        if device_count > 0:
            raise SiteHasDevicesError(site_id, device_count)
        # Order matters under M3's RESTRICT FK: null the site's back-reference
        # to its Default group *first*, then delete groups (the group's site
        # FK is RESTRICT too, but sites.default_group_id → group RESTRICT is
        # what would fire otherwise). Finally drop the site itself.
        row.default_group_id = None
        session.flush()
        session.query(DeviceGroupModel).filter_by(site_id=site_id).delete(
            synchronize_session=False
        )
        session.flush()
        session.delete(row)
    logger.info("Site deleted: id=%s", site_id)
    return True


def ensure_base_infrastructure() -> int:
    """MSP: Phase 1 — idempotent bootstrap of the Base-Infrastructure Site
    and its Default DeviceGroup.

    Called from ``app.main`` at startup. Safe to invoke on every boot: no-op
    if both rows already exist. Returns the Base-Infrastructure Site id.

    Runs INSERT site → INSERT group → UPDATE site.default_group_id inside
    one ``get_session()`` transaction so a partial state can never become
    visible.
    """
    with get_session() as session:
        site = (
            session.query(SiteModel)
            .filter_by(kind=BASE_INFRA_SITE_KIND)
            .first()
        )
        if site is None:
            site = SiteModel(
                name=BASE_INFRA_SITE_NAME,
                kind=BASE_INFRA_SITE_KIND,
                description="System-managed base infrastructure site.",
            )
            session.add(site)
            session.flush()  # populate site.id before referencing it
            logger.info("Base-Infrastructure site created (id=%s)", site.id)

        if site.default_group_id is None:
            existing_default = (
                session.query(DeviceGroupModel)
                .filter_by(site_id=site.id, is_default=True)
                .first()
            )
            if existing_default is None:
                group = DeviceGroupModel(
                    name=DEFAULT_GROUP_NAME,
                    site_id=site.id,
                    is_default=True,
                    description=f"Default group for {BASE_INFRA_SITE_NAME}.",
                )
                session.add(group)
                session.flush()
                site.default_group_id = group.id
                logger.info(
                    "Base-Infrastructure default group created (id=%s)",
                    group.id,
                )
            else:
                site.default_group_id = existing_default.id
                logger.info(
                    "Base-Infrastructure default group already exists (id=%s); linked",
                    existing_default.id,
                )

        return site.id
