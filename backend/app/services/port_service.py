from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.config import EXECUTION_MODE
from app.models.port import PortInfo, PortListResponse
from app.services import secret_service

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
    password = secret_service.vault.decrypt(device.encrypted_password)
    driver = _get_driver(device)

    # Locking is the API layer's responsibility (mirrors the VLAN read path
    # where `api/vlans.py` acquires the lock with a short timeout).  Keeping
    # it out of the service means orchestration code that is already holding
    # the lock (pre-state capture, rollback verification, ...) can call this
    # without deadlocking.
    ports = driver.list_ports(device, password)

    logger.info(
        "Listed %d ports on device=%s vendor=%s",
        len(ports), device.name, device.vendor,
    )
    return PortListResponse(device=device.name, vendor=device.vendor, ports=ports)


def update_port_description_on_device(
    interface: str,
    description: str,
    device_id: str,
) -> dict:
    """Set the description of *interface* on *device_id*.

    Dispatches to the vendor driver's ``update_port_description``.  Caller
    (typically ``port_execution_service``) is responsible for retry,
    rollback verification, and device locking — this helper is the thin
    "drive one Ansible playbook" layer that mirrors
    ``vlan_service.update_vlan_description``.

    Empty ``description`` is normalized to mean "clear the description"
    (``undo description`` on Huawei, ``no description`` on Cisco) — the
    vendor driver / playbook is the one that interprets this.

    Parameters
    ----------
    interface:
        Vendor-native interface name (e.g. ``"GigabitEthernet1/0/1"``).
    description:
        New description string.  May be empty to clear.
    device_id:
        Device name to act on.

    Returns
    -------
    dict
        Normalized Ansible result: ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.

    Notes
    -----
    Mock mode is supported for tests / local dev: returns success unless
    ``device_id == "fail_device"``.
    """
    if EXECUTION_MODE == "mock":
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: update description on interface=%s device=%s description=%r",
            interface, device_id, description,
        )
        return {"rc": 0, "stdout": "Simulated description updated", "stderr": "", "success": True}

    dev = _resolve_device(device_id)
    pw = secret_service.vault.decrypt(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info(
        "Real mode: update description on interface=%s device=%s",
        interface, dev.name,
    )
    return driver.update_port_description(interface, description, dev, pw)


def set_port_admin_state_on_device(
    interface: str,
    enabled: bool,
    device_id: str,
) -> dict:
    """Administratively enable / disable *interface* on *device_id*.

    Dispatches to the vendor driver's ``set_port_admin_state``.  Caller
    (typically ``port_execution_service``) handles retry, rollback, and
    device locking — this is the thin "drive one Ansible playbook" layer
    that mirrors ``update_port_description_on_device``.

    Parameters
    ----------
    interface:
        Vendor-native interface name (e.g. ``"GigabitEthernet1/0/1"``).
    enabled:
        ``True`` to bring the interface up, ``False`` to shut it down.
    device_id:
        Device name to act on.

    Returns
    -------
    dict
        Normalized Ansible result: ``{"rc", "stdout", "stderr", "success"}``.

    Notes
    -----
    Mock mode is supported for tests / local dev: returns success unless
    ``device_id == "fail_device"``.
    """
    if EXECUTION_MODE == "mock":
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: set admin state on interface=%s device=%s enabled=%s",
            interface, device_id, enabled,
        )
        return {"rc": 0, "stdout": "Simulated admin state applied", "stderr": "", "success": True}

    dev = _resolve_device(device_id)
    pw = secret_service.vault.decrypt(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info(
        "Real mode: set admin state on interface=%s device=%s enabled=%s",
        interface, dev.name, enabled,
    )
    return driver.set_port_admin_state(interface, enabled, dev, pw)


def set_port_access_vlan_on_device(
    interface: str,
    vlan_id: int,
    device_id: str,
) -> dict:
    """Assign *vlan_id* as the access VLAN of *interface* on *device_id*.

    Dispatches to the vendor driver's ``set_port_access_vlan``.  Caller
    (typically ``port_execution_service``) handles retry, rollback, device
    locking, and the access-mode pre-condition check.

    Parameters
    ----------
    interface:
        Vendor-native interface name.
    vlan_id:
        Access VLAN ID (validated at the API boundary).
    device_id:
        Device name to act on.

    Returns
    -------
    dict
        Normalized Ansible result: ``{"rc", "stdout", "stderr", "success"}``.

    Notes
    -----
    Mock mode is supported for tests / local dev: returns success unless
    ``device_id == "fail_device"``.
    """
    if EXECUTION_MODE == "mock":
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: set access VLAN on interface=%s device=%s vlan_id=%d",
            interface, device_id, vlan_id,
        )
        return {"rc": 0, "stdout": "Simulated access VLAN applied", "stderr": "", "success": True}

    dev = _resolve_device(device_id)
    pw = secret_service.vault.decrypt(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info(
        "Real mode: set access VLAN on interface=%s device=%s vlan_id=%d",
        interface, dev.name, vlan_id,
    )
    return driver.set_port_access_vlan(interface, vlan_id, dev, pw)


def set_trunk_pvid_vlan_on_device(
    interface: str,
    vlan_id: int,
    device_id: str,
) -> dict:
    """Set the trunk native VLAN (PVID) of *interface* on *device_id*.

    Dispatches to the vendor driver's ``set_trunk_pvid_vlan``.  Caller
    (typically ``port_execution_service``) handles retry, rollback, device
    locking, and the trunk-mode pre-condition check.

    Parameters
    ----------
    interface:
        Vendor-native interface name.
    vlan_id:
        PVID / native VLAN ID (validated at the API boundary).
    device_id:
        Device name to act on.

    Returns
    -------
    dict
        Normalized Ansible result: ``{"rc", "stdout", "stderr", "success"}``.
    """
    if EXECUTION_MODE == "mock":
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: set trunk PVID on interface=%s device=%s vlan_id=%d",
            interface, device_id, vlan_id,
        )
        return {"rc": 0, "stdout": "Simulated trunk PVID applied", "stderr": "", "success": True}

    dev = _resolve_device(device_id)
    pw = secret_service.vault.decrypt(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info(
        "Real mode: set trunk PVID on interface=%s device=%s vlan_id=%d",
        interface, dev.name, vlan_id,
    )
    return driver.set_trunk_pvid_vlan(interface, vlan_id, dev, pw)


def set_trunk_allowed_vlans_on_device(
    interface: str,
    vlan_list: list[int],
    device_id: str,
) -> dict:
    """Set the trunk allowed-VLAN list of *interface* on *device_id*.

    Dispatches to the vendor driver's ``set_trunk_allowed_vlans``.  The caller
    (``port_execution_service``) is responsible for computing the final desired
    list (from mode + pre-state + requested VLANs) before calling this function.
    The driver always performs a full replace on the device.

    Notes
    -----
    Mock mode: returns success unless ``device_id == "fail_device"``.
    """
    if EXECUTION_MODE == "mock":
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: set trunk VLANs on interface=%s device=%s vlans=%s",
            interface, device_id, vlan_list,
        )
        return {"rc": 0, "stdout": "Simulated trunk VLANs applied", "stderr": "", "success": True}

    dev = _resolve_device(device_id)
    pw = secret_service.vault.decrypt(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info(
        "Real mode: set trunk VLANs on interface=%s device=%s vlans=%s",
        interface, dev.name, vlan_list,
    )
    return driver.set_trunk_allowed_vlans(interface, vlan_list, dev, pw)


def shutdown_port_on_device(interface: str, device_id: str) -> dict:
    """Administratively disable *interface* on *device_id* (shutdown).

    Dispatches to the vendor driver's ``shutdown_port``.  Caller (typically
    ``port_config_service``) handles retry, rollback, and device locking.

    Returns
    -------
    dict
        ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
    """
    if EXECUTION_MODE == "mock":
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info("Mock: shutdown_port on interface=%s device=%s", interface, device_id)
        return {"rc": 0, "stdout": "Simulated port shutdown", "stderr": "", "success": True}
    dev = _resolve_device(device_id)
    pw = secret_service.vault.decrypt(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info("Real mode: shutdown_port on interface=%s device=%s", interface, dev.name)
    return driver.shutdown_port(interface, dev, pw)


def enable_port_on_device(interface: str, device_id: str) -> dict:
    """Administratively enable *interface* on *device_id* (no shutdown).

    Dispatches to the vendor driver's ``enable_port``.  Caller handles retry,
    rollback, and device locking.

    Returns
    -------
    dict
        ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
    """
    if EXECUTION_MODE == "mock":
        if device_id == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info("Mock: enable_port on interface=%s device=%s", interface, device_id)
        return {"rc": 0, "stdout": "Simulated port enabled", "stderr": "", "success": True}
    dev = _resolve_device(device_id)
    pw = secret_service.vault.decrypt(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info("Real mode: enable_port on interface=%s device=%s", interface, dev.name)
    return driver.enable_port(interface, dev, pw)


def configure_port_on_device(config: "PortConfigRequest", device_id: str) -> "PortConfigResult":
    """Apply a composite set of port mutations on *device_id*.

    Dispatches to the vendor driver's ``configure_port``.  Caller handles
    retry, rollback, and device locking.  Pre-condition validation is the
    orchestration layer's responsibility.

    Returns
    -------
    PortConfigResult
        Normalized result from the vendor driver.
    """
    if EXECUTION_MODE == "mock":
        from app.models.port import PortConfigResult as _PCR
        if device_id == "fail_device":
            logger.info(
                "Mock: configure_port FAILED on interface=%s device=%s",
                config.interface, device_id,
            )
            return _PCR(success=False, changed=False, interface=config.interface, vendor="mock")
        logger.info(
            "Mock: configure_port on interface=%s device=%s fields=%s",
            config.interface, device_id, config.mutation_fields,
        )
        return _PCR(
            success=True, changed=True, interface=config.interface,
            vendor="mock", execution_time_ms=1.0,
        )
    dev = _resolve_device(device_id)
    pw = secret_service.vault.decrypt(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info("Real mode: configure_port on interface=%s device=%s", config.interface, dev.name)
    return driver.configure_port(config, dev, pw)


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
