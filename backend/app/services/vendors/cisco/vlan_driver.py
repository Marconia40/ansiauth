from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.services import ansible_service
from app.services.vendors.base import BaseVendorDriver

if TYPE_CHECKING:
    from app.models.vlan import VLANInfo

logger = logging.getLogger(__name__)

_NETWORK_OS = "ios"
_CONNECTION = "network_cli"

_PLAYBOOK_CREATE = "vendors/cisco/create_vlan.yml"
_PLAYBOOK_DELETE = "vendors/cisco/delete_vlan.yml"
_PLAYBOOK_GET = "vendors/cisco/get_vlans.yml"
_PLAYBOOK_UPDATE = "vendors/cisco/update_vlan.yml"


class CiscoVlanDriver(BaseVendorDriver):
    """Vendor driver for Cisco IOS devices.

    Selects Cisco-specific playbooks, executes them via ansible_service,
    and normalizes all outputs to the standard result format.
    """

    def create_vlan(self, vlan_id: int, name: str, device, password: str) -> dict:
        logger.info(
            "CiscoVlanDriver.create_vlan vlan_id=%s device=%s",
            vlan_id, device.name,
        )
        inventory = ansible_service.build_inventory(
            device.name, device.host, device.username, password,
            network_os=_NETWORK_OS, connection=_CONNECTION,
        )
        return ansible_service.run_playbook(
            playbook=_PLAYBOOK_CREATE,
            extravars={"vlan_id": vlan_id, "vlan_name": name, "device": device.name},
            inventory=inventory,
        )

    def delete_vlan(self, vlan_id: int, device, password: str) -> dict:
        logger.info(
            "CiscoVlanDriver.delete_vlan vlan_id=%s device=%s",
            vlan_id, device.name,
        )
        inventory = ansible_service.build_inventory(
            device.name, device.host, device.username, password,
            network_os=_NETWORK_OS, connection=_CONNECTION,
        )
        return ansible_service.run_playbook(
            playbook=_PLAYBOOK_DELETE,
            extravars={"vlan_id": vlan_id, "device": device.name},
            inventory=inventory,
        )

    def update_vlan(self, vlan_id: int, name: str, device, password: str) -> dict:
        logger.info(
            "CiscoVlanDriver.update_vlan vlan_id=%s device=%s",
            vlan_id, device.name,
        )
        inventory = ansible_service.build_inventory(
            device.name, device.host, device.username, password,
            network_os=_NETWORK_OS, connection=_CONNECTION,
        )
        return ansible_service.run_playbook(
            playbook=_PLAYBOOK_UPDATE,
            extravars={"vlan_id": vlan_id, "description": name, "device": device.name},
            inventory=inventory,
        )

    def get_vlans(self, device, password: str) -> list[VLANInfo]:
        logger.info("CiscoVlanDriver.get_vlans device=%s", device.name)
        inventory = ansible_service.build_inventory(
            device.name, device.host, device.username, password,
            network_os=_NETWORK_OS, connection=_CONNECTION,
        )
        result = ansible_service.run_playbook(
            playbook=_PLAYBOOK_GET,
            extravars={"device": device.name},
            inventory=inventory,
        )
        if result["rc"] != 0:
            error = result.get("stderr") or result.get("stdout") or "get_vlans playbook failed"
            raise RuntimeError(error)
        from app.services.parsers.vlan_parser import parse_vlan_brief
        return parse_vlan_brief(result["stdout"])

    def save_config(self, device, password: str) -> dict:
        """Cisco IOS commits changes immediately; no explicit save step is required."""
        raise NotImplementedError(
            f"save_config is not supported for Cisco IOS device '{device.name}'. "
            "Cisco IOS writes changes to the running config automatically."
        )
