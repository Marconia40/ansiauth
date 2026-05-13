import logging

from app.services import ansible_service
from app.services.vendors.base import BaseVendorDriver

logger = logging.getLogger(__name__)

_NETWORK_OS = "ce"
_CONNECTION = "network_cli"

_PLAYBOOK_CREATE = "vendors/huawei/create_vlan.yml"
_PLAYBOOK_DELETE = "vendors/huawei/delete_vlan.yml"
_PLAYBOOK_GET = "vendors/huawei/get_vlans.yml"
_PLAYBOOK_UPDATE = "vendors/huawei/update_vlan.yml"


class HuaweiVlanDriver(BaseVendorDriver):
    """Vendor driver for Huawei VRP devices.

    Uses community.network.ce_* Ansible modules and normalizes
    all outputs to the standard result format.
    """

    def create_vlan(self, vlan_id: int, name: str, device, password: str) -> dict:
        logger.info("HuaweiVlanDriver.create_vlan vlan_id=%s device=%s", vlan_id, device.name)
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
        logger.info("HuaweiVlanDriver.delete_vlan vlan_id=%s device=%s", vlan_id, device.name)
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
        logger.info("HuaweiVlanDriver.update_vlan vlan_id=%s device=%s", vlan_id, device.name)
        inventory = ansible_service.build_inventory(
            device.name, device.host, device.username, password,
            network_os=_NETWORK_OS, connection=_CONNECTION,
        )
        return ansible_service.run_playbook(
            playbook=_PLAYBOOK_UPDATE,
            extravars={"vlan_id": vlan_id, "description": name, "device": device.name},
            inventory=inventory,
        )

    def get_vlans(self, device, password: str) -> list[dict]:
        logger.info("HuaweiVlanDriver.get_vlans device=%s", device.name)
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
        from app.services.parsers.vlan_parser import parse_vrp_vlan_display
        return parse_vrp_vlan_display(result["stdout"])
