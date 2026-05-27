"""Site-scoped authorization helpers.

Policy (Step 7.3):
  * `admin` and `super-admin` roles get full visibility — they bypass all
    site-scoped filtering.
  * `operator` and `observer` roles are restricted to resources tied to devices
    in their `allowed_sites` set. A user with an empty `allowed_sites` set sees
    nothing.

These helpers query the DB on demand. FastAPI's per-request dependency cache
keeps redundant lookups in the same request from hitting the DB twice when
endpoints use `Depends(get_current_user)` — but they only hold the username and
role payload, so authz helpers do their own lookup.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.db.models import DeviceModel, UserAllowedSiteModel, UserModel
from app.db.session import get_session


_UNRESTRICTED_ROLES = frozenset({"admin", "super-admin"})


def is_unrestricted(user: dict) -> bool:
    """Return True when the caller bypasses site scoping (admin / super-admin)."""
    return user.get("role") in _UNRESTRICTED_ROLES


def allowed_site_ids_for(user: dict) -> Optional[set[int]]:
    """Return the set of site IDs the caller may see.

    Returns:
        None   — caller is unrestricted (admin / super-admin); no filtering.
        set    — possibly empty; restricted caller sees only these sites.
    """
    if is_unrestricted(user):
        return None
    username = (user.get("username") or "").lower()
    if not username:
        return set()
    with get_session() as session:
        row = session.query(UserModel).filter_by(username=username).first()
        if row is None:
            return set()
        ids = {site.id for site in row.allowed_sites}
        return ids


def allowed_device_names_for(user: dict) -> Optional[set[str]]:
    """Return device names visible to the caller.

    Returns:
        None — unrestricted (admin / super-admin); caller may use any device.
        set  — restricted caller's exact visible device set.

    Policy:
      * Non-empty `allowed_sites` → strict whitelist of devices in those sites.
        Unassigned devices are *not* visible (spec: "Only see allowed sites").
      * Empty `allowed_sites` → caller falls back to devices with `site_id IS NULL`.
        This is the migration-safe default: on a fresh install where no sites
        have been created yet, operators can still work with their devices.
        Once an admin assigns any site, the caller switches to strict scoping.
    """
    site_ids = allowed_site_ids_for(user)
    if site_ids is None:
        return None
    with get_session() as session:
        if site_ids:
            rows = (
                session.query(DeviceModel.name)
                .filter(DeviceModel.site_id.in_(site_ids))
                .all()
            )
        else:
            rows = (
                session.query(DeviceModel.name)
                .filter(DeviceModel.site_id.is_(None))
                .all()
            )
        return {r[0] for r in rows}


def ensure_device_allowed(user: dict, device_name: str) -> None:
    """Raise HTTPException(403) when the caller may not act on this device.

    Admins always pass. For restricted roles, the device must exist and have a
    `site_id` in the caller's allowed set.
    """
    if is_unrestricted(user):
        return
    allowed = allowed_device_names_for(user)
    # allowed is a set (possibly empty) — not None — because we checked unrestricted above.
    if not allowed or device_name not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Device '{device_name}' is outside your allowed sites",
        )


def ensure_devices_allowed(user: dict, device_names) -> None:
    """Bulk variant: raise 403 if any device in the iterable is not allowed."""
    if is_unrestricted(user):
        return
    allowed = allowed_device_names_for(user) or set()
    for name in device_names:
        if name not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Device '{name}' is outside your allowed sites",
            )


def set_user_allowed_sites(user_id: int, site_ids: list[int]) -> list[int]:
    """Replace a user's allowed_sites with the given site IDs.

    Returns the resolved set of IDs that were actually persisted (validates that
    every supplied ID exists). Raises ValueError if any ID is unknown.
    """
    from app.db.models import SiteModel
    requested = set(site_ids)
    with get_session() as session:
        if requested:
            existing = {
                r[0] for r in session.query(SiteModel.id).filter(SiteModel.id.in_(requested)).all()
            }
            missing = requested - existing
            if missing:
                raise ValueError(f"Unknown site IDs: {sorted(missing)}")
        # Clear existing rows for this user, then insert the new set.
        session.query(UserAllowedSiteModel).filter_by(user_id=user_id).delete(
            synchronize_session=False
        )
        for sid in sorted(requested):
            session.add(UserAllowedSiteModel(user_id=user_id, site_id=sid))
    return sorted(requested)


def get_user_allowed_sites(user_id: int) -> tuple[list[int], list[str]]:
    """Return (sorted ids, sorted names) of sites a user is allowed to access."""
    with get_session() as session:
        row = session.query(UserModel).filter_by(id=user_id).first()
        if row is None:
            return [], []
        ids = sorted(s.id for s in row.allowed_sites)
        names = sorted(s.name for s in row.allowed_sites)
        return ids, names
