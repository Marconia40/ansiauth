import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from app.db.models import DeviceModel, SiteModel
from app.db.session import get_session
from app.models.device import Device
from app.services import secret_service

logger = logging.getLogger(__name__)

_VALID_VENDORS = {"cisco_ios", "cisco", "huawei"}


def _to_domain(row: DeviceModel) -> Device:
    """Project a persisted device row onto the domain model.

    Site attributes are derived through ``device.device_group.site`` — the
    only site pointer devices carry after Phase 5.
    """
    group = getattr(row, "device_group", None)
    group_id = group.id if group is not None else None
    group_name = group.name if group is not None else None
    site_id = group.site_id if group is not None else None
    site_name = group.site.name if group is not None and group.site is not None else None
    return Device(
        name=row.name,
        host=row.host,
        vendor=row.vendor,
        platform=row.platform or "ios",  # default for rows added before platform existed
        username=row.username,
        encrypted_password=row.encrypted_password,
        id=str(row.id),
        created_at=row.created_at or datetime.now(timezone.utc),
        site_id=site_id,
        site_name=site_name,
        device_group_id=group_id,
        device_group_name=group_name,
    )


def _validate_site_or_raise(session, site_id: int) -> None:
    if not session.query(SiteModel).filter_by(id=site_id).first():
        raise ValueError(f"Site {site_id} not found")


def create_device(
    name: str,
    host: str,
    vendor: str,
    username: str,
    password: str,
    platform: str = "ios",
    site_id: int | None = None,
    device_group_id: int | None = None,
) -> Device:
    """Service-level device create used by ``Inventory.register``, tests, and
    seed scripts. ``devices.device_group_id`` is NOT NULL — when neither
    ``site_id`` nor ``device_group_id`` is supplied the row lands in the
    auto-provisioned ``Mock Site``'s Default group.
    """
    if vendor not in _VALID_VENDORS:
        raise ValueError(f"Vendor '{vendor}' not supported. Valid values: {', '.join(sorted(_VALID_VENDORS))}")
    encrypted = secret_service.encrypt_password(password)
    with get_session() as session:
        if site_id is not None:
            _validate_site_or_raise(session, site_id)
        resolved_group_id = _resolve_default_group(
            session, site_id=site_id, device_group_id=device_group_id,
        )
        row = DeviceModel(
            name=name,
            host=host,
            vendor=vendor,
            platform=platform,
            username=username,
            encrypted_password=encrypted,
            device_group_id=resolved_group_id,
            created_at=datetime.now(timezone.utc),
        )
        try:
            session.add(row)
            session.flush()  # get auto-assigned id before commit
            domain = _to_domain(row)
        except IntegrityError:
            raise ValueError(f"Device '{name}' already exists")
    logger.info("Device %s created at %s", name, host)
    return domain


def _resolve_default_group(session, *, site_id: int | None, device_group_id: int | None) -> int:
    """Return the ``device_group_id`` a new device should land in.

    Preference order:
      1. ``device_group_id`` provided → use it (validated to exist).
      2. Only ``site_id`` → that site's Default group.
      3. Neither → Mock Site's Default group (seed/test fallback).
    """
    from app.db.models import DeviceGroupModel

    if device_group_id is not None:
        row = session.query(
            DeviceGroupModel.id, DeviceGroupModel.site_id,
        ).filter_by(id=device_group_id).first()
        if row is None:
            raise ValueError(f"Device group {device_group_id} not found")
        return row[0]

    if site_id is not None:
        default_gid = session.query(SiteModel.default_group_id).filter_by(
            id=site_id,
        ).scalar()
        if default_gid is None:
            raise ValueError(f"Site {site_id} has no Default group")
        return default_gid

    # Neither — fall back to Mock Site (created by seed_defaults).
    row = session.query(SiteModel.default_group_id).filter_by(
        name="Mock Site",
    ).first()
    if row is None or row[0] is None:
        raise ValueError(
            "No site_id/device_group_id provided and Mock Site is missing. "
            "Provide a site_id or device_group_id, or run seed_defaults."
        )
    return row[0]


def get_device(name: str) -> Device | None:
    with get_session() as session:
        row = session.query(DeviceModel).filter_by(name=name).first()
        return _to_domain(row) if row else None


def get_devices() -> list[Device]:
    with get_session() as session:
        rows = session.query(DeviceModel).all()
        return [_to_domain(r) for r in rows]


def update_device(
    name: str,
    host: str | None = None,
    vendor: str | None = None,
    platform: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> Device:
    """Update mutable connection details on a device.

    A device's owning group (and, transitively, its site) is changed via
    ``POST /devices/{name}/move`` — never through this endpoint.
    """
    with get_session() as session:
        row = session.query(DeviceModel).filter_by(name=name).first()
        if not row:
            raise ValueError(f"Device '{name}' not found")
        if host is not None:
            row.host = host
        if vendor is not None:
            if vendor not in _VALID_VENDORS:
                raise ValueError(f"Vendor '{vendor}' not supported. Valid values: {', '.join(sorted(_VALID_VENDORS))}")
            row.vendor = vendor
        if platform is not None:
            row.platform = platform
        if username is not None:
            row.username = username
        if password is not None:
            row.encrypted_password = secret_service.encrypt_password(password)
        session.flush()
        domain = _to_domain(row)
    logger.info("Device %s updated", name)
    return domain


def delete_device(name: str) -> Device | None:
    with get_session() as session:
        row = session.query(DeviceModel).filter_by(name=name).first()
        if not row:
            return None
        domain = _to_domain(row)
        session.delete(row)
    logger.info("Device %s removed", name)
    return domain


def seed_defaults() -> None:
    """Insert mock_device and fail_device if they don't already exist.

    The mock devices live in a REGULAR site (auto-created on first call) so
    operator/observer users granted "every REGULAR site" (see conftest's
    ``_seed_test_role_users_with_full_visibility``) can see them. Placing
    them in Base-Infrastructure would hide them from every non system-admin
    (D14). The site is idempotently created here; it is fine to leave in
    place for demo/dev environments.
    """
    from app.services.site_service import (
        DEFAULT_GROUP_NAME,
        REGULAR_SITE_KIND,
        ensure_base_infrastructure,
    )
    from app.db.models import DeviceGroupModel
    # Idempotent — the Base-Infra site+group are created lazily on first boot.
    ensure_base_infrastructure()
    mock_site_name = "Mock Site"
    with get_session() as session:
        row = session.query(SiteModel).filter_by(name=mock_site_name).first()
        if row is None:
            row = SiteModel(name=mock_site_name, kind=REGULAR_SITE_KIND,
                            description="Auto-created site for seed_defaults mock devices.")
            session.add(row)
            session.flush()
            group = DeviceGroupModel(
                name=DEFAULT_GROUP_NAME, site_id=row.id, is_default=True,
                description=f"Default group for site '{mock_site_name}'.",
            )
            session.add(group)
            session.flush()
            row.default_group_id = group.id
            session.flush()
            logger.info("Seeded mock REGULAR site (id=%s) + default group (id=%s)",
                        row.id, group.id)
        mock_default_group_id = row.default_group_id
    if mock_default_group_id is None:
        logger.warning("seed_defaults: Mock Site missing its default group; skipping devices.")
        return
    defaults = [
        ("mock_device", "192.168.1.1"),
        ("fail_device", "192.168.1.2"),
    ]
    for name, host in defaults:
        with get_session() as session:
            if not session.query(DeviceModel).filter_by(name=name).first():
                session.add(DeviceModel(
                    name=name, host=host, vendor="cisco_ios", platform="ios",
                    username="admin",
                    encrypted_password=secret_service.encrypt_password("admin"),
                    device_group_id=mock_default_group_id,
                    created_at=datetime.now(timezone.utc),
                ))
                logger.info("Seeded default device: %s (group=%s)", name, mock_default_group_id)


def clear_devices() -> None:
    """Delete all devices and re-seed defaults. Used in tests."""
    with get_session() as session:
        session.query(DeviceModel).delete()
    logger.info("All devices cleared")
    seed_defaults()
