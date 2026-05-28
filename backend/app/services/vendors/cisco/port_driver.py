from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING

from app.services import ansible_service
from app.services.parsers.cisco_port_parser import parse_ios_ports
from app.services.vendors.port_driver_base import BasePortDriver

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.port import PortInfo

logger = logging.getLogger(__name__)

_NETWORK_OS = "ios"
_CONNECTION = "network_cli"

_PLAYBOOK_GET_PORTS = "vendors/cisco/get_ports.yml"
_PLAYBOOK_UPDATE_DESCRIPTION = "vendors/cisco/update_port_description.yml"
_PLAYBOOK_SET_ADMIN_STATE = "vendors/cisco/set_port_admin_state.yml"

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
