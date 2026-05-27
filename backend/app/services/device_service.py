import logging
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from app.db.models import DeviceModel, SiteModel
from app.db.session import get_session
from app.models.device import Device
from app.services import secret_service

logger = logging.getLogger(__name__)

_VALID_VENDORS = {"cisco_ios", "cisco", "huawei"}

# Sentinel to differentiate "no change" from "explicitly clear to None" in updates.
_UNSET = object()


def _to_domain(row: DeviceModel) -> Device:
    # row.site is eager-loadable via the relationship; access here while the session is open.
    site_name = row.site.name if row.site is not None else None
    return Device(
        name=row.name,
        host=row.host,
        vendor=row.vendor,
        platform=row.platform or "ios",  # default for rows added before platform existed
        username=row.username,
        encrypted_password=row.encrypted_password,
        id=str(row.id),
        created_at=row.created_at or datetime.now(timezone.utc),
        site_id=row.site_id,
        site_name=site_name,
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
) -> Device:
    if vendor not in _VALID_VENDORS:
        raise ValueError(f"Vendor '{vendor}' not supported. Valid values: {', '.join(sorted(_VALID_VENDORS))}")
    encrypted = secret_service.encrypt_password(password)
    row = DeviceModel(
        name=name,
        host=host,
        vendor=vendor,
        platform=platform,
        username=username,
        encrypted_password=encrypted,
        site_id=site_id,
        created_at=datetime.now(timezone.utc),
    )
    try:
        with get_session() as session:
            if site_id is not None:
                _validate_site_or_raise(session, site_id)
            session.add(row)
            session.flush()  # get auto-assigned id before commit
            domain = _to_domain(row)
    except IntegrityError:
        raise ValueError(f"Device '{name}' already exists")
    logger.info("Device %s created at %s", name, host)
    return domain


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
    site_id=_UNSET,  # sentinel: unset → don't touch; None → clear; int → assign
) -> Device:
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
        if site_id is not _UNSET:
            if site_id is not None:
                _validate_site_or_raise(session, site_id)
            row.site_id = site_id
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
    """Insert mock_device and fail_device if they don't already exist."""
    defaults = [
        ("mock_device", "192.168.1.1"),
        ("fail_device", "192.168.1.2"),
    ]
    for name, host in defaults:
        with get_session() as session:
            if not session.query(DeviceModel).filter_by(name=name).first():
                session.add(DeviceModel(
                    name=name,
                    host=host,
                    vendor="cisco_ios",
                    platform="ios",
                    username="admin",
                    encrypted_password=secret_service.encrypt_password("admin"),
                    created_at=datetime.now(timezone.utc),
                ))
                logger.info("Seeded default device: %s", name)


def clear_devices() -> None:
    """Delete all devices and re-seed defaults. Used in tests."""
    with get_session() as session:
        session.query(DeviceModel).delete()
    logger.info("All devices cleared")
    seed_defaults()
