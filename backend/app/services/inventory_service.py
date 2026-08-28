"""MSP: Phase 3 — Inventory service.

Sole entry point for Device CRUD, listing and movement. Every endpoint that
mutates ``devices.device_group_id`` (including the legacy group ``add_member``
/ ``remove_member`` compat paths) routes through this service so that:

  * The invariant "a device belongs to exactly one group" cannot be broken by
    a stray writer.
  * Cross-cutting rules (D_active_job, D8 auto-move-to-default) live in one
    place.
  * Audit rows for device motion are shaped uniformly.

Do NOT add non-Device operations here — service growth is capped at the five
methods below by the phase-3 acceptance criteria.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    DeviceGroupModel,
    DeviceModel,
    JobModel,
    SiteModel,
)
from app.db.session import get_session
from app.models.device import Device
from app.services import audit_service, secret_service

logger = logging.getLogger(__name__)

_NON_TERMINAL_JOB_STATUSES = {"pending", "running"}

_VALID_VENDORS = {"cisco_ios", "cisco", "huawei"}


class Inventory:
    """Sole entry point for Device CRUD, listing, and movement."""

    def __init__(self, db=None):
        # ``db`` remains for API/plan compatibility, but every method opens its
        # own ``get_session()`` context — matches the codebase's session-per-
        # call convention. Passing a session in is currently a no-op.
        self._db = db

    # ─── Read ────────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[Device]:
        from app.services import device_service
        return device_service.get_device(name)

    def list(
        self,
        user: dict,
        *,
        site_id: Optional[int] = None,
        device_group_id: Optional[int] = None,
    ) -> list[Device]:
        """Return devices visible to the caller, optionally filtered by site
        or group. Visibility comes from ``role_assignments`` — site-wide and
        group-specific grants unioned."""
        from app.services import device_service

        visible = self._visible_device_names(user, site_id=site_id, device_group_id=device_group_id)
        if visible is None:
            # None → unrestricted (system-admin)
            return device_service.get_devices()
        if not visible:
            return []
        all_devices = device_service.get_devices()
        subset = [d for d in all_devices if d.name in visible]
        if site_id is not None:
            subset = [d for d in subset if d.site_id == site_id]
        if device_group_id is not None:
            subset = [d for d in subset if d.device_group_id == device_group_id]
        return subset

    # ─── Write ───────────────────────────────────────────────────────────

    def register(
        self,
        *,
        name: str,
        host: str,
        vendor: str,
        platform: str,
        username: str,
        password: str,
        site_id: int,
        device_group_id: Optional[int],
        actor: dict,
    ) -> Device:
        """Create a new device attached to ``device_group_id`` (or the site's
        Default group when ``device_group_id`` is None)."""
        from app.services import device_service

        if vendor not in _VALID_VENDORS:
            raise ValueError(
                f"Vendor '{vendor}' not supported. Valid values: {', '.join(sorted(_VALID_VENDORS))}"
            )
        encrypted = secret_service.encrypt_password(password)
        with get_session() as session:
            site = session.query(SiteModel).filter_by(id=site_id).first()
            if site is None:
                raise ValueError(f"Site {site_id} not found")
            target_group_id = device_group_id
            if target_group_id is None:
                target_group_id = site.default_group_id
                if target_group_id is None:
                    raise ValueError(
                        f"Site {site_id} has no Default group; cannot register device"
                    )
            group = session.query(DeviceGroupModel).filter_by(id=target_group_id).first()
            if group is None:
                raise ValueError(f"Device group {target_group_id} not found")
            if group.site_id != site_id:
                raise ValueError(
                    f"Device group {target_group_id} belongs to site {group.site_id}, "
                    f"not site {site_id}"
                )
            row = DeviceModel(
                name=name,
                host=host,
                vendor=vendor,
                platform=platform,
                username=username,
                encrypted_password=encrypted,
                device_group_id=target_group_id,
                created_at=datetime.now(timezone.utc),
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                raise ValueError(f"Device '{name}' already exists")
            domain = device_service._to_domain(row)
        audit_service.log_action(
            user=(actor.get("username") if actor else None) or "system",
            action="register_device",
            resource="device",
            resource_id=str(domain.id),
            details={
                "name": name,
                "host": host,
                "vendor": vendor,
                "site_id": site_id,
                "device_group_id": target_group_id,
            },
            device=name,
        )
        logger.info("Inventory.register: %s → group=%s site=%s", name, target_group_id, site_id)
        return domain

    def move(
        self,
        name: str,
        target_group_id: Optional[int],
        actor: dict,
        *,
        reason: Optional[str] = None,
        enforce_authz: bool = True,
    ) -> Device:
        """Move a device to a new group.

        Semantics
        ---------
        * ``target_group_id`` is an int  → move the device into that group.
          The target may be in the same site (D16 → operator on the device)
          or a different site (D16 → admin on both sides). ``require_scope
          ("move_device")`` enforces the correct role *before* this method
          runs; ``enforce_authz=False`` skips the double-check when the
          caller has already been authorized (e.g. auto-move on group delete).
        * ``target_group_id`` is None    → D8: move the device to its current
          site's Default group. Used both by the API layer (``{"device_group
          _id": null}``) and by the legacy ``remove_member`` compat shim.

        Guards
        ------
        * Raises HTTP 404 if the device does not exist.
        * Raises HTTP 409 (D_active_job) if the device has a non-terminal
          Job — moves must not race with in-flight config changes.
        """
        from app.services import device_service

        # Load + validate under a single transaction so the D_active_job
        # check and the FK swap are atomic. A non-terminal job created
        # between check-and-swap would still be caught by the audit trail.
        with get_session() as session:
            row = session.query(DeviceModel).filter_by(name=name).first()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Device '{name}' not found")

            source_group = row.device_group
            source_group_id = source_group.id if source_group is not None else None
            source_site_id = source_group.site_id if source_group is not None else None
            if source_site_id is None:
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Device '{name}' is not attached to a group; refusing "
                        "to move — data-integrity invariant violated"
                    ),
                )

            # Resolve target — None means D8 "reset to current site's Default".
            if target_group_id is None:
                default_group_id = (
                    session.query(SiteModel.default_group_id)
                    .filter(SiteModel.id == source_site_id)
                    .scalar()
                )
                if default_group_id is None:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Site {source_site_id} has no Default group",
                    )
                resolved_group_id = int(default_group_id)
            else:
                resolved_group_id = int(target_group_id)

            target_group = (
                session.query(DeviceGroupModel).filter_by(id=resolved_group_id).first()
            )
            if target_group is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Target device group {resolved_group_id} does not exist",
                )
            if target_group.site_id is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Target device group {resolved_group_id} is not attached to a site"
                    ),
                )
            target_site_id = int(target_group.site_id)

            # No-op moves succeed silently — keeps the endpoint idempotent
            # without emitting a spurious audit row.
            if source_group_id == resolved_group_id:
                return device_service._to_domain(row)

            # D_active_job guard: reject when a Job is pending/running for
            # this device to avoid config divergence mid-move.
            active = (
                session.query(JobModel.job_id, JobModel.status)
                .filter(
                    JobModel.device == name,
                    JobModel.status.in_(_NON_TERMINAL_JOB_STATUSES),
                )
                .first()
            )
            if active is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot move device '{name}': job {active[0]} is "
                        f"currently {active[1]}. Wait for it to finish or cancel it."
                    ),
                )

            # Apply the move on the authoritative FK.
            row.device_group_id = resolved_group_id
            session.flush()
            # Refresh so ``row.device_group`` reflects the new FK — otherwise
            # ``_to_domain`` would report the pre-move group in the response.
            session.expire(row, ["device_group"])
            _ = row.device_group  # trigger lazy reload while session is open
            domain = device_service._to_domain(row)

        audit_service.log_action(
            user=(actor.get("username") if actor else None) or "system",
            action="move_device",
            resource="device",
            resource_id=str(domain.id),
            device=name,
            details={
                "name": name,
                "from_group_id": source_group_id,
                "from_site_id": source_site_id,
                "to_group_id": resolved_group_id,
                "to_site_id": target_site_id,
                "cross_site": source_site_id != target_site_id,
                "reason": reason,
                "resolved_from_default": target_group_id is None,
            },
        )
        logger.info(
            "Inventory.move: %s → group=%s (site=%s, cross_site=%s, reason=%s)",
            name, resolved_group_id, target_site_id,
            source_site_id != target_site_id, reason,
        )
        return domain

    def deregister(self, name: str, actor: dict) -> None:
        """Delete a device. Rejects the delete if a Job is still in flight."""
        with get_session() as session:
            row = session.query(DeviceModel).filter_by(name=name).first()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Device '{name}' not found")
            active = (
                session.query(JobModel.job_id, JobModel.status)
                .filter(
                    JobModel.device == name,
                    JobModel.status.in_(_NON_TERMINAL_JOB_STATUSES),
                )
                .first()
            )
            if active is not None:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Cannot delete device '{name}': job {active[0]} is "
                        f"currently {active[1]}."
                    ),
                )
            session.delete(row)
        audit_service.log_action(
            user=(actor.get("username") if actor else None) or "system",
            action="delete_device",
            resource="device",
            details={"name": name},
            device=name,
        )
        logger.info("Inventory.deregister: %s", name)

    # ─── Internal helpers ────────────────────────────────────────────────

    def _visible_device_names(
        self,
        user: dict,
        *,
        site_id: Optional[int],
        device_group_id: Optional[int],
    ) -> Optional[set[str]]:
        """Return the set of device names the caller can see under MSP rules.

        Returns ``None`` for unrestricted callers (``is_system_admin``) so the
        caller can skip the ``in`` filter entirely. Returns an empty set for
        callers with zero grants.
        """
        if user.get("is_system_admin"):
            return None
        user_id = user.get("id")
        if user_id is None:
            return set()
        from app.db.models import RoleAssignmentModel
        with get_session() as session:
            grants = (
                session.query(
                    RoleAssignmentModel.site_id,
                    RoleAssignmentModel.device_group_id,
                )
                .filter(RoleAssignmentModel.user_id == user_id)
                .all()
            )
            if not grants:
                return set()
            site_wide = {sid for (sid, gid) in grants if gid is None}
            group_specific = {gid for (_sid, gid) in grants if gid is not None}
            # Devices in any site-wide-granted site …
            q = session.query(DeviceModel.name).join(
                DeviceGroupModel, DeviceModel.device_group_id == DeviceGroupModel.id
            )
            from sqlalchemy import or_
            conds = []
            if site_wide:
                conds.append(DeviceGroupModel.site_id.in_(site_wide))
            if group_specific:
                conds.append(DeviceModel.device_group_id.in_(group_specific))
            if not conds:
                return set()
            q = q.filter(or_(*conds))
            if site_id is not None:
                q = q.filter(DeviceGroupModel.site_id == site_id)
            if device_group_id is not None:
                q = q.filter(DeviceModel.device_group_id == device_group_id)
            return {r[0] for r in q.all()}
