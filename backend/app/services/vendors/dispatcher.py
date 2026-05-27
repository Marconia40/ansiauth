from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.services.vendors.base import BaseVendorDriver
from app.services.vendors.port_driver_base import BasePortDriver

if TYPE_CHECKING:
    from app.models.device import Device

logger = logging.getLogger(__name__)

# To add a new vendor:
#   1. Create app/services/vendors/<vendor>/vlan_driver.py with a class that
#      extends BaseVendorDriver and implements all abstract methods.
#   2. Add the vendor string(s) to a new frozenset below.
#   3. Add an ``if vendor in _<VENDOR>_VENDORS:`` branch in get_vendor_driver.
#   4. For port-management support, create app/services/vendors/<vendor>/port_driver.py
#      extending BasePortDriver and wire it into get_port_driver below.
#   No changes are required anywhere else in the service or API layers.
_CISCO_VENDORS = frozenset({"cisco", "cisco_ios"})
_HUAWEI_VENDORS = frozenset({"huawei", "huawei_vrp"})


def get_driver(device: Device) -> BaseVendorDriver:
    """Return the appropriate VLAN driver for *device*.

    Preferred entry point for all application code.  Callers pass the full
    device domain object and remain vendor-agnostic; all vendor resolution
    logic stays here in the dispatcher.

    Parameters
    ----------
    device:
        Domain device object exposing .name, .vendor, .platform.

    Returns
    -------
    BaseVendorDriver
        Concrete driver instance ready to execute VLAN operations.

    Raises
    ------
    ValueError
        If no driver is registered for device.vendor / device.platform.
    """
    logger.info(
        "Resolving driver for device=%s vendor=%s platform=%s",
        device.name, device.vendor, device.platform,
    )
    return get_vendor_driver(device.vendor, device.platform)


def get_vendor_driver(vendor: str, platform: str) -> BaseVendorDriver:
    """Return the driver for the given vendor/platform strings.

    This is the internal routing core.  Application code should call
    ``get_driver(device)`` instead so that vendor details stay encapsulated
    here.  This function remains public for callers that only have
    vendor/platform strings (e.g. direct test fixtures or admin utilities).

    Parameters
    ----------
    vendor:
        Vendor identifier string (e.g. ``"cisco_ios"``, ``"huawei_vrp"``).
    platform:
        Platform identifier string (e.g. ``"ios"``, ``"vrp"``).

    Returns
    -------
    BaseVendorDriver
        Concrete driver instance.

    Raises
    ------
    ValueError
        If no driver is registered for the given vendor/platform pair.
    """
    if vendor in _CISCO_VENDORS:
        from app.services.vendors.cisco.vlan_driver import CiscoVlanDriver
        return CiscoVlanDriver()
    if vendor in _HUAWEI_VENDORS:
        from app.services.vendors.huawei.vlan_driver import HuaweiVlanDriver
        return HuaweiVlanDriver()
    raise ValueError(f"No driver registered for vendor='{vendor}' platform='{platform}'")


def get_port_driver(device: Device) -> BasePortDriver:
    """Return the appropriate port driver for *device*.

    Mirrors ``get_driver`` (VLAN) so the port-management service layer can
    stay fully vendor-agnostic.  Step 1.1 ships the Huawei implementation
    only; a future Cisco port driver will register here without touching
    callers.

    Parameters
    ----------
    device:
        Domain device object exposing .name, .vendor, .platform.

    Returns
    -------
    BasePortDriver
        Concrete read-only port driver instance.

    Raises
    ------
    ValueError
        If no port driver is registered for device.vendor / device.platform.
    """
    logger.info(
        "Resolving port driver for device=%s vendor=%s platform=%s",
        device.name, device.vendor, device.platform,
    )
    return get_port_vendor_driver(device.vendor, device.platform)


def get_port_vendor_driver(vendor: str, platform: str) -> BasePortDriver:
    """Return the port driver for the given vendor / platform strings.

    Internal routing core for port drivers.  Application code should prefer
    ``get_port_driver(device)`` so vendor strings stay encapsulated here.

    Raises
    ------
    ValueError
        If no port driver is registered for the given vendor / platform pair.
    """
    if vendor in _HUAWEI_VENDORS:
        from app.services.vendors.huawei.port_driver import HuaweiPortDriver
        return HuaweiPortDriver()
    # Cisco port driver intentionally not registered yet (Step 1.1 ships
    # Huawei first per the project's multi-vendor directive).
    raise ValueError(
        f"No port driver registered for vendor='{vendor}' platform='{platform}'"
    )
