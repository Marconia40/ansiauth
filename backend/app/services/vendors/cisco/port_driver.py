from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING

from app.services import ansible_service
from app.services.parsers.cisco_port_parser import parse_ios_ports
from app.services.vendors.port_driver_base import BasePortDriver

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.port import PortConfigRequest, PortConfigResult, PortInfo

logger = logging.getLogger(__name__)

_NETWORK_OS = "ios"
_CONNECTION = "network_cli"

_PLAYBOOK_GET_PORTS = "vendors/cisco/get_ports.yml"
_PLAYBOOK_UPDATE_DESCRIPTION = "vendors/cisco/update_port_description.yml"
_PLAYBOOK_SET_ADMIN_STATE = "vendors/cisco/set_port_admin_state.yml"
_PLAYBOOK_SET_ACCESS_VLAN = "vendors/cisco/set_access_vlan.yml"
_PLAYBOOK_SET_TRUNK_VLANS = "vendors/cisco/set_trunk_allowed_vlans.yml"
_PLAYBOOK_SET_TRUNK_PVID = "vendors/cisco/set_trunk_pvid.yml"

# The Cisco get_ports playbook issues a single ios_command task with three
# commands in this order.  ios_command returns stdout as a list, which is
# expanded into the `stdouts` array by `_extract_all_command_outputs`.
#   0. show interfaces status
#   1. show interfaces description
#   2. show interfaces switchport
# If the playbook is changed to emit a different order, these indices must
# be updated alongside it.
_STATUS_INDEX = 0
_DESCRIPTION_INDEX = 1
_SWITCHPORT_INDEX = 2


def _build_inventory(device: Device, password: str) -> str:
    return ansible_service.build_inventory(
        device.name, device.host, device.username, password,
        network_os=_NETWORK_OS, connection=_CONNECTION,
    )


class CiscoPortDriver(BasePortDriver):
    """Read-only Cisco IOS port driver (Step 1.3).

    Issues three ``show`` commands in a single ``ios_command`` task and
    merges their outputs into normalized ``PortInfo`` objects via
    ``parse_ios_ports``.

    Why three commands rather than one
    ----------------------------------
    No single Cisco IOS read command exposes all of: admin/operational
    state, untruncated description, and the L2 switchport profile.  Issuing
    them as one ``ios_command`` task is cheap (single SSH session, single
    runner invocation) and keeps the parser modular and tolerant of partial
    output — a missing description response degrades to ``description=None``
    for every port rather than failing the whole call.

    Mutation operations (enable/disable, description change, VLAN
    assignment, ...) are intentionally absent — they belong to later steps.
    """

    def list_ports(self, device: Device, password: str) -> list[PortInfo]:
        """Return all physical switchports on *device* as ``PortInfo`` objects.

        Filters out routed L3 interfaces, SVIs, loopbacks, tunnels, and
        management ports.  Fields the chosen read commands do not expose
        (PoE / speed / duplex in Step 1.3) are left as ``None`` per the
        "do not invent values" rule.

        Parameters
        ----------
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        list[PortInfo]
            Normalized port inventory, sorted by interface name.

        Raises
        ------
        RuntimeError
            If the playbook fails or returns output that cannot be parsed.
        """
        logger.info("Listing ports on device=%s", device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_GET_PORTS,
                extravars={"device": device.name},
                inventory=_build_inventory(device, password),
            )
        except Exception as exc:
            logger.exception(
                "FULL CISCO TRACEBACK [list_ports device=%s]: %s\n%s",
                device.name, str(exc), traceback.format_exc(),
            )
            raise

        if result["rc"] != 0:
            error = result.get("stderr") or result.get("stdout") or "playbook exited non-zero"
            logger.error(
                "Cisco command failed: get_ports on device=%s — %s",
                device.name, error,
            )
            raise RuntimeError(
                f"Cannot determine port state on device '{device.name}': {error}"
            )

        stdouts = result.get("stdouts") or []
        if not stdouts:
            logger.error(
                "Cisco get_ports on device=%s returned no command outputs",
                device.name,
            )
            raise RuntimeError(
                f"Cannot determine port state on device '{device.name}': "
                "no command outputs returned by playbook"
            )

        status = stdouts[_STATUS_INDEX] if len(stdouts) > _STATUS_INDEX else ""
        description = stdouts[_DESCRIPTION_INDEX] if len(stdouts) > _DESCRIPTION_INDEX else ""
        switchport = stdouts[_SWITCHPORT_INDEX] if len(stdouts) > _SWITCHPORT_INDEX else ""

        logger.debug(
            "Cisco get_ports raw lengths on device=%s: status=%d desc=%d switchport=%d",
            device.name, len(status), len(description), len(switchport),
        )

        try:
            ports = parse_ios_ports(status, description, switchport)
        except Exception as exc:
            logger.exception(
                "FULL CISCO TRACEBACK [list_ports parse device=%s]: %s\n%s",
                device.name, str(exc), traceback.format_exc(),
            )
            raise RuntimeError(
                f"Cannot determine port state on device '{device.name}': {exc}"
            ) from exc

        logger.info(
            "Cisco port parser returned %d interfaces on device=%s",
            len(ports), device.name,
        )
        return ports

    # ── Mutation operations ──────────────────────────────────────────────────

    def update_port_description(
        self,
        interface: str,
        description: str,
        device: Device,
        password: str,
    ) -> dict:
        """Set the port description on a Cisco IOS device.

        Runs ``cisco.ios.ios_config`` inside the interface parent context.
        Empty / whitespace-only ``description`` triggers ``no description``
        rather than echoing an empty literal — keeping the device's
        running config clean.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        is_empty = not bool(description and description.strip())
        logger.info(
            "Cisco: update description on interface=%s device=%s (clear=%s)",
            interface, device.name, is_empty,
        )
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_UPDATE_DESCRIPTION,
                extravars={
                    "interface": interface,
                    "description": description or "",
                    "description_is_empty": is_empty,
                    "device": device.name,
                },
                inventory=_build_inventory(device, password),
            )
            normalized = {**result, "success": result.get("rc", 1) == 0}
            if normalized["success"]:
                logger.info(
                    "Cisco: update description OK on interface=%s device=%s",
                    interface, device.name,
                )
            else:
                logger.error(
                    "Cisco: update description FAILED on interface=%s device=%s — %s",
                    interface, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL CISCO TRACEBACK [update_port_description interface=%s device=%s]: %s\n%s",
                interface, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def set_port_admin_state(
        self,
        interface: str,
        enabled: bool,
        device: Device,
        password: str,
    ) -> dict:
        """Administratively enable / disable *interface* on a Cisco IOS device.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        logger.info(
            "Cisco: set admin state on interface=%s device=%s enabled=%s",
            interface, device.name, enabled,
        )
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_SET_ADMIN_STATE,
                extravars={
                    "interface": interface,
                    "enabled": bool(enabled),
                    "device": device.name,
                },
                inventory=_build_inventory(device, password),
            )
            normalized = {**result, "success": result.get("rc", 1) == 0}
            if normalized["success"]:
                logger.info(
                    "Cisco: set admin state OK on interface=%s device=%s enabled=%s",
                    interface, device.name, enabled,
                )
            else:
                logger.error(
                    "Cisco: set admin state FAILED on interface=%s device=%s enabled=%s — %s",
                    interface, device.name, enabled,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL CISCO TRACEBACK [set_port_admin_state interface=%s device=%s]: %s\n%s",
                interface, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def set_port_access_vlan(
        self,
        interface: str,
        vlan_id: int,
        device: Device,
        password: str,
    ) -> dict:
        """Set the access VLAN of *interface* on a Cisco IOS device.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        logger.info(
            "Cisco: set access VLAN on interface=%s device=%s vlan_id=%d",
            interface, device.name, vlan_id,
        )
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_SET_ACCESS_VLAN,
                extravars={
                    "interface": interface,
                    "vlan_id": int(vlan_id),
                    "device": device.name,
                },
                inventory=_build_inventory(device, password),
            )
            normalized = {**result, "success": result.get("rc", 1) == 0}
            if normalized["success"]:
                logger.info(
                    "Cisco: set access VLAN OK on interface=%s device=%s vlan_id=%d",
                    interface, device.name, vlan_id,
                )
            else:
                logger.error(
                    "Cisco: set access VLAN FAILED on interface=%s device=%s vlan_id=%d — %s",
                    interface, device.name, vlan_id,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL CISCO TRACEBACK [set_port_access_vlan interface=%s device=%s]: %s\n%s",
                interface, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def set_trunk_pvid_vlan(
        self,
        interface: str,
        vlan_id: int,
        device: Device,
        password: str,
    ) -> dict:
        """Set the trunk native VLAN of *interface* on a Cisco IOS device.

        Runs ``switchport trunk native vlan {{ vlan_id }}`` inside the interface
        context.  The orchestration layer must guarantee the port is already
        in trunk mode before this is called.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        logger.info(
            "Cisco: set trunk native VLAN on interface=%s device=%s vlan_id=%d",
            interface, device.name, vlan_id,
        )
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_SET_TRUNK_PVID,
                extravars={
                    "interface": interface,
                    "vlan_id": int(vlan_id),
                    "device": device.name,
                },
                inventory=_build_inventory(device, password),
            )
            normalized = {**result, "success": result.get("rc", 1) == 0}
            if normalized["success"]:
                logger.info(
                    "Cisco: set trunk native VLAN OK on interface=%s device=%s vlan_id=%d",
                    interface, device.name, vlan_id,
                )
            else:
                logger.error(
                    "Cisco: set trunk native VLAN FAILED on interface=%s device=%s vlan_id=%d — %s",
                    interface, device.name, vlan_id,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL CISCO TRACEBACK [set_trunk_pvid_vlan interface=%s device=%s]: %s\n%s",
                interface, device.name, str(exc), traceback.format_exc(),
            )
            raise

    # ── Step 3.1 composite / semantic stubs ─────────────────────────────────────

    def configure_port(
        self,
        config: PortConfigRequest,
        device: Device,
        password: str,
    ) -> PortConfigResult:
        """Composite port configuration — Step 3.1 stub (not yet implemented)."""
        raise NotImplementedError(
            "CiscoPortDriver.configure_port is not yet implemented — "
            "use the individual set_port_* methods for now"
        )

    def shutdown_port(
        self,
        interface: str,
        device: Device,
        password: str,
    ) -> dict:
        """Shut down *interface* — Step 3.1 stub (not yet implemented)."""
        raise NotImplementedError(
            "CiscoPortDriver.shutdown_port is not yet implemented — "
            "use set_port_admin_state(enabled=False) for now"
        )

    def enable_port(
        self,
        interface: str,
        device: Device,
        password: str,
    ) -> dict:
        """Enable *interface* — Step 3.1 stub (not yet implemented)."""
        raise NotImplementedError(
            "CiscoPortDriver.enable_port is not yet implemented — "
            "use set_port_admin_state(enabled=True) for now"
        )

    def set_trunk_allowed_vlans(
        self,
        interface: str,
        vlan_list: list[int],
        device: Device,
        password: str,
    ) -> dict:
        """Set the trunk allowed-VLAN list of *interface* on a Cisco IOS device.

        Runs ``switchport trunk allowed vlan <list>`` inside the interface
        context.  IOS replaces the current list with the new one in a single
        atomic command.  ``save_when: always`` persists to startup-config.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        from app.validators.port_validator import compress_vlans_cisco
        vlan_str = compress_vlans_cisco(sorted(set(vlan_list)))
        logger.info(
            "Cisco: set trunk VLANs on interface=%s device=%s vlans=%s",
            interface, device.name, vlan_str,
        )
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_SET_TRUNK_VLANS,
                extravars={
                    "interface": interface,
                    "vlan_list": vlan_str,
                    "device": device.name,
                },
                inventory=_build_inventory(device, password),
            )
            normalized = {**result, "success": result.get("rc", 1) == 0}
            if normalized["success"]:
                logger.info(
                    "Cisco: set trunk VLANs OK on interface=%s device=%s vlans=%s",
                    interface, device.name, vlan_str,
                )
            else:
                logger.error(
                    "Cisco: set trunk VLANs FAILED on interface=%s device=%s vlans=%s — %s",
                    interface, device.name, vlan_str,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL CISCO TRACEBACK [set_trunk_allowed_vlans interface=%s device=%s]: %s\n%s",
                interface, device.name, str(exc), traceback.format_exc(),
            )
            raise
