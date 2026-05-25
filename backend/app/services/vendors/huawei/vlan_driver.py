import logging
import traceback

from app.services import ansible_service
from app.services.vendors.base import BaseVendorDriver

logger = logging.getLogger(__name__)

_NETWORK_OS = "community.network.ce"
_CONNECTION = "network_cli"

_PLAYBOOK_CREATE = "vendors/huawei/create_vlan.yml"
_PLAYBOOK_DELETE = "vendors/huawei/delete_vlan.yml"
_PLAYBOOK_GET = "vendors/huawei/get_vlans.yml"
_PLAYBOOK_UPDATE = "vendors/huawei/update_vlan.yml"
_PLAYBOOK_SAVE = "vendors/huawei/save_config.yml"


class HuaweiVlanDriver(BaseVendorDriver):
    """Vendor driver for Huawei VRP devices.

    Uses ansible.netcommon.cli_command / cli_config modules over network_cli
    with community.network.ce as the terminal plugin. This avoids the
    ce_command JSON-decode failure that occurs when VRP devices return plain
    CLI text rather than structured output.
    """

    def create_vlan(self, vlan_id: int, name: str, device, password: str) -> dict:
        logger.info("Executing Huawei command: create VLAN %s on device=%s", vlan_id, device.name)
        try:
            inventory = ansible_service.build_inventory(
                device.name, device.host, device.username, password,
                network_os=_NETWORK_OS, connection=_CONNECTION,
            )
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_CREATE,
                extravars={"vlan_id": vlan_id, "vlan_name": name, "device": device.name},
                inventory=inventory,
            )
            if result["rc"] == 0:
                logger.info("Huawei command completed successfully: create VLAN %s on device=%s", vlan_id, device.name)
            else:
                logger.error("Huawei command failed: create VLAN %s on device=%s — %s", vlan_id, device.name, result.get("stderr") or result.get("stdout"))
            return result
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [create_vlan vlan_id=%s device=%s]: %s\n%s",
                vlan_id, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def delete_vlan(self, vlan_id: int, device, password: str) -> dict:
        logger.info("Executing Huawei command: delete VLAN %s on device=%s", vlan_id, device.name)
        try:
            inventory = ansible_service.build_inventory(
                device.name, device.host, device.username, password,
                network_os=_NETWORK_OS, connection=_CONNECTION,
            )
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_DELETE,
                extravars={"vlan_id": vlan_id, "device": device.name},
                inventory=inventory,
            )
            if result["rc"] == 0:
                logger.info("Huawei command completed successfully: delete VLAN %s on device=%s", vlan_id, device.name)
            else:
                logger.error("Huawei command failed: delete VLAN %s on device=%s — %s", vlan_id, device.name, result.get("stderr") or result.get("stdout"))
            return result
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [delete_vlan vlan_id=%s device=%s]: %s\n%s",
                vlan_id, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def update_vlan(self, vlan_id: int, name: str, device, password: str) -> dict:
        logger.info("Executing Huawei command: update VLAN %s on device=%s", vlan_id, device.name)
        try:
            inventory = ansible_service.build_inventory(
                device.name, device.host, device.username, password,
                network_os=_NETWORK_OS, connection=_CONNECTION,
            )
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_UPDATE,
                extravars={"vlan_id": vlan_id, "description": name, "device": device.name},
                inventory=inventory,
            )
            if result["rc"] == 0:
                logger.info("Huawei command completed successfully: update VLAN %s on device=%s", vlan_id, device.name)
            else:
                logger.error("Huawei command failed: update VLAN %s on device=%s — %s", vlan_id, device.name, result.get("stderr") or result.get("stdout"))
            return result
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [update_vlan vlan_id=%s device=%s]: %s\n%s",
                vlan_id, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def get_vlans(self, device, password: str) -> list[dict]:
        logger.info("Executing Huawei command: display vlan on device=%s", device.name)
        try:
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
                error = result.get("stderr") or result.get("stdout") or "playbook exited non-zero"
                logger.error("Huawei command failed: display vlan on device=%s — %s", device.name, error)
                raise RuntimeError(f"Cannot determine VLAN state on device '{device.name}': {error}")

            stdout = result.get("stdout", "")
            if not stdout or not stdout.strip():
                logger.error("Huawei command failed: display vlan on device=%s returned empty output", device.name)
                raise RuntimeError(f"Cannot determine VLAN state on device '{device.name}': empty output from display vlan")

            logger.debug("Huawei display vlan raw output (%d chars) on device=%s: %.500s", len(stdout), device.name, stdout)

            try:
                from app.services.parsers.vlan_parser import parse_vrp_vlan_display
                vlans = parse_vrp_vlan_display(stdout)
                logger.info("Huawei VLAN parse returned %d VLANs on device=%s", len(vlans), device.name)
                return vlans
            except Exception as exc:
                logger.exception(
                    "FULL HUAWEI TRACEBACK [get_vlans parse device=%s]: %s\n%s",
                    device.name, str(exc), traceback.format_exc(),
                )
                raise RuntimeError(f"Cannot determine VLAN state on device '{device.name}': {exc}") from exc
        except RuntimeError:
            raise
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [get_vlans device=%s]: %s\n%s",
                device.name, str(exc), traceback.format_exc(),
            )
            raise

    def save_config(self, device, password: str) -> dict:
        logger.info("Executing Huawei command: save force on device=%s", device.name)
        try:
            inventory = ansible_service.build_inventory(
                device.name, device.host, device.username, password,
                network_os=_NETWORK_OS, connection=_CONNECTION,
            )
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_SAVE,
                extravars={"device": device.name},
                inventory=inventory,
            )
            if result["rc"] == 0:
                logger.info("Huawei command completed successfully: save force on device=%s", device.name)
            else:
                logger.error("Huawei command failed: save force on device=%s — %s", device.name, result.get("stderr") or result.get("stdout"))
            return result
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [save_config device=%s]: %s\n%s",
                device.name, str(exc), traceback.format_exc(),
            )
            raise
