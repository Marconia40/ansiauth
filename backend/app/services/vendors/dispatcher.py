import logging

from app.services.vendors.base import BaseVendorDriver

logger = logging.getLogger(__name__)

_CISCO_VENDORS = frozenset({"cisco", "cisco_ios"})
_HUAWEI_VENDORS = frozenset({"huawei", "huawei_vrp"})


def get_vendor_driver(vendor: str, platform: str) -> BaseVendorDriver:
    """Return the driver for the given vendor/platform pair.

    This is the single place where vendor resolution lives. Callers never
    branch on vendor strings themselves — they always go through here.
    """
    logger.info("Using vendor driver vendor=%s platform=%s", vendor, platform)
    if vendor in _CISCO_VENDORS:
        from app.services.vendors.cisco.vlan_driver import CiscoVlanDriver
        return CiscoVlanDriver()
    if vendor in _HUAWEI_VENDORS:
        from app.services.vendors.huawei.vlan_driver import HuaweiVlanDriver
        return HuaweiVlanDriver()
    raise ValueError(f"No driver registered for vendor='{vendor}' platform='{platform}'")
