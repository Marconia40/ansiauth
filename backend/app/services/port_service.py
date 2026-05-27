from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.config import EXECUTION_MODE
from app.models.port import PortInfo, PortListResponse
from app.services import device_locks, secret_service

if TYPE_CHECKING:
    from app.services.vendors.port_driver_base import BasePortDriver

logger = logging.getLogger(__name__)


# ── Mock data ────────────────────────────────────────────────────────────────

_INITIAL_MOCK_PORTS: list[PortInfo] = [
    PortInfo(
        name="GigabitEthernet0/0/1",
        description="Workstation-01",
        admin_up=True,
        operational_up=True,
        mode="access",
        access_vlan=10,
        allowed_vlans=None,
    ),
    PortInfo(
        name="GigabitEthernet0/0/2",
        description="Workstation-02",
        admin_up=True,
        operational_up=False,
        mode="access",
        access_vlan=20,
        allowed_vlans=None,
    ),
    PortInfo(
        name="GigabitEthernet0/0/24",
        description="Uplink to core",
        admin_up=True,
        operational_up=True,
        mode="trunk",
        access_vlan=1,
        allowed_vlans=[10, 20, 30],
    ),
]


def _mock_list_ports(device_id: str) -> list[PortInfo]:
    """Return a deterministic list of mock ports for unit / dev runs."""
    if device_id == "fail_device":
        raise RuntimeError(f"Simulated port listing failure on device '{device_id}'")
    logger.info("Mock: returning %d ports for device=%s", len(_INITIAL_MOCK_PORTS), device_id)
    # Defensive copies so mutation by callers cannot leak into the next call.
    return [PortInfo.from_dict(p.to_dict()) for p in _INITIAL_MOCK_PORTS]


# ── Device resolution & driver dispatch ──────────────────────────────────────

def _resolve_device(device_id: str):
    from app.services.device_service import get_device
    device = get_device(device_id)
    if not device:
        raise ValueError(f"Device '{device_id}' not found")
    return device


def _get_driver(device) -> BasePortDriver:
    """Resolve the port driver for *device* via the dispatcher.

    Keeps the service layer free of vendor strings — all routing is in the
    dispatcher.  Lazy import avoids forcing every consumer of port_service
    to also pull in the vendor module graph at startup.
    """
    from app.services.vendors.dispatcher import get_port_driver
    return get_port_driver(device)


# ── Public API ───────────────────────────────────────────────────────────────

def list_ports(device_id: str) -> PortListResponse:
    """Return the normalized port inventory of *device_id*.

    Orchestration shape
    -------------------
    * Resolves the device record (raises ``ValueError`` on unknown id).
    * Acquires the per-device lock so the read serializes safely against
      concurrent VLAN mutations on the same device.
    * Decrypts the stored credentials and dispatches to the appropriate
      vendor port driver.
    * Wraps the driver's ``list[PortInfo]`` in a ``PortListResponse``
      carrying device + vendor provenance for API consumers.

    Mock mode bypasses the driver entirely and returns ``_INITIAL_MOCK_PORTS``.

    Parameters
    ----------
    device_id:
        Device name to query.  Must match a record in the device service.

    Returns
    -------
    PortListResponse
        Envelope with ``device``, ``vendor`` and the sorted ``ports`` list.

    Raises
    ------
    ValueError
        If *device_id* is not registered.
    RuntimeError
        If the underlying playbook fails or returns unparseable output.
    TimeoutError
        If the per-device lock cannot be acquired (busy device).
    """
    logger.info("Listing ports on device=%s mode=%s", device_id, EXECUTION_MODE)

    if EXECUTION_MODE == "mock":
        ports = _mock_list_ports(device_id)
        return PortListResponse(device=device_id, vendor="mock", ports=ports)

    device = _resolve_device(device_id)
    password = secret_service.decrypt_password(device.encrypted_password)
    driver = _get_driver(device)

    with device_locks.acquire(device.name):
        ports = driver.list_ports(device, password)

    logger.info(
        "Listed %d ports on device=%s vendor=%s",
        len(ports), device.name, device.vendor,
    )
    return PortListResponse(device=device.name, vendor=device.vendor, ports=ports)


def get_port(device_id: str, name: str) -> PortInfo | None:
    """Return a single ``PortInfo`` for *name* on *device_id*, or ``None`` if absent.

    Convenience wrapper around ``list_ports`` for callers that only need
    one interface.  In Step 1.1 this performs a full list under the hood;
    future vendor drivers may override the corresponding base-class hook
    to fetch a single port directly when the underlying CLI supports it.

    Parameters
    ----------
    device_id:
        Device name to query.
    name:
        Interface identifier (e.g. ``"GigabitEthernet0/0/1"``).

    Returns
    -------
    PortInfo | None
    """
    response = list_ports(device_id)
    return next((p for p in response.ports if p.name == name), None)
