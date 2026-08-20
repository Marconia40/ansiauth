import logging
from typing import Optional

from app.db.models import DeviceGroupModel, DeviceModel, SiteModel
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
    with get_session() as session:
        if session.query(SiteModel).filter_by(name=name).first():
            raise ValueError(f"Site '{name}' already exists")
        row = SiteModel(name=name, description=description)
        session.add(row)
        session.flush()
        result = _to_read(row, session)
    logger.info("Site created: name=%s", name)
    return result


def get_site(site_id: int) -> Optional[SiteRead]:
    with get_session() as session:
        row = session.query(SiteModel).filter_by(id=site_id).first()
        return _to_read(row, session) if row else None


def list_sites() -> list[SiteRead]:
    with get_session() as session:
        rows = session.query(SiteModel).order_by(SiteModel.name).all()
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
        device_count = session.query(DeviceModel).filter_by(site_id=site_id).count()
        if device_count > 0:
            raise SiteHasDevicesError(site_id, device_count)
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
