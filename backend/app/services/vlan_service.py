from __future__ import annotations

import logging

from app.core.config import EXECUTION_MODE
from app.models.vlan import VLAN
from app.services.inventory_service import Inventory

# Shared with services/vendors/mock.py's MockVlanDriver (D1b) — Device routes
# all VLAN mock traffic through that module now. This module's own
# ``_mock_vlans`` (kept for api/vlans.py's device-less read path, see
# get_vlans() below) mutates the SAME list object so both paths stay
# consistent instead of drifting into two independent mock "databases".
from app.services.vendors.mock import _mock_vlans, reset_mock_vlans  # noqa: F401

logger = logging.getLogger(__name__)


def _get_device(device_id: str):
    """Resolve *device_id* to a ``Device`` via ``Inventory``, or raise.

    Preserves the exact ``ValueError`` contract the old ``_resolve_device``
    had — callers (api/vlans.py) catch ``ValueError`` to produce a clean 404.
    """
    device = Inventory().get(device_id)
    if device is None:
        raise ValueError(f"Device '{device_id}' not found")
    return device


# ── Public API — one-line delegates to Device (DG3) ───────────────────────────
#
# These functions used to each repeat the same resolve → decrypt → dispatch
# dance (see docs/DEVICE_IMPLEMENTATION_PLAN.md §3). That logic now lives in
# Device itself (models/device.py) — kept here as thin delegates, not deleted
# outright, because dozens of tests monkeypatch these exact functions by name
# to simulate real-mode execution (module-attribute patches, which still work
# unchanged regardless of what the real implementation below does).

def create_vlan_on_device(vlan_id: int, name: str, device_id: str) -> dict:
    """Create a VLAN on a single named device. Used for multi-device execution."""
    return _get_device(device_id).create_vlan(VLAN(vlan_id=vlan_id, name=name))


def delete_vlan(vlan_id: int, device_id: str) -> dict:
    return _get_device(device_id).delete_vlan(VLAN(vlan_id=vlan_id))


def update_vlan_description(vlan_id: int, description: str, device_id: str) -> dict:
    return _get_device(device_id).update_vlan_description(VLAN(vlan_id=vlan_id, name=description))


def get_vlans(device_id: str | None = None) -> list[VLAN]:
    """Return VLANs configured on *device_id* as normalized ``VLAN`` objects.

    ``device_id=None`` is only valid in mock mode — returns the flat
    in-memory ``_mock_vlans`` list directly (api/vlans.py's "no device
    specified" convenience). Otherwise delegates to ``Device.list_vlans()``.

    Callers at the API boundary must convert to dicts via
    ``[v.to_dict() for v in get_vlans(device_id)]`` before including the
    result in HTTP responses.
    """
    if device_id is None:
        if EXECUTION_MODE == "mock":
            logger.info("Mock: returning hardcoded VLAN list")
            return _mock_vlans
        raise ValueError("device_id is required in real mode")
    return _get_device(device_id).list_vlans()


def save_config_on_device(device_id: str) -> dict:
    return _get_device(device_id).save_config()


def vlan_exists(device_id: str, vlan_id: int) -> bool:
    """Return True if the given VLAN is already configured on the device."""
    try:
        vlans = get_vlans(device_id)
        return any(v.vlan_id == vlan_id for v in vlans)
    except Exception as exc:
        logger.warning(
            "vlan_exists check failed for device=%s vlan=%s: %s — treating as unknown",
            device_id, vlan_id, exc,
        )
        return False
