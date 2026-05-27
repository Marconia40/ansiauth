from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING

from app.services import ansible_service
from app.services.parsers.port_parser import parse_vrp_ports
from app.services.vendors.port_driver_base import BasePortDriver

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.port import PortInfo

logger = logging.getLogger(__name__)

_NETWORK_OS = "community.network.ce"
_CONNECTION = "network_cli"

_PLAYBOOK_GET_PORTS = "vendors/huawei/get_ports.yml"

# The Huawei get_ports playbook issues three cli_command tasks in this order:
#   0. display interface brief
#   1. display interface description
#   2. display port vlan
# The driver consumes them via ansible_service's stdouts list (ordered by
# task completion).  If a future schema change reorders these tasks, this
# mapping must be updated alongside the playbook.
_BRIEF_INDEX = 0
_DESCRIPTION_INDEX = 1
_PORT_VLAN_INDEX = 2


def _build_inventory(device: Device, password: str) -> str:
    return ansible_service.build_inventory(
        device.name, device.host, device.username, password,
        network_os=_NETWORK_OS, connection=_CONNECTION,
    )


class HuaweiPortDriver(BasePortDriver):
    """Read-only Huawei VRP port driver (Step 1.1).

    Issues three ``display`` commands in a single playbook and merges them
    into normalized ``PortInfo`` objects via ``parse_vrp_ports``.

    Mutation operations (enable/disable, description change, VLAN
    assignment, etc.) are intentionally absent — they belong to later steps.

    Why three commands rather than one
    ---------------------------------
    ``display interface`` would expose admin state, oper state, description,
    speed and duplex per port but does not surface the L2 mode / PVID /
    allowed-VLAN-list in a stable, easily-parseable form across VRP releases.
    ``display port vlan`` provides that L2 view cleanly but lacks admin /
    oper state and description.  Issuing three lightweight ``display``
    commands and merging in Python keeps the parser modular and tolerant of
    individual command failures (a missing description response degrades to
    ``description=None`` for every port rather than failing the whole call).
    """

    def list_ports(self, device: Device, password: str) -> list[PortInfo]:
        """Return all physical switchports on *device* as ``PortInfo`` objects.

        Filters out pseudo-interfaces (SVIs, NULL0, loopbacks, eth-trunks).
        Fields the chosen read commands do not expose (PoE, speed, duplex
        in Step 1.1) are left as ``None`` per the "do not invent values"
        rule.

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
                "FULL HUAWEI TRACEBACK [list_ports device=%s]: %s\n%s",
                device.name, str(exc), traceback.format_exc(),
            )
            raise

        if result["rc"] != 0:
            error = result.get("stderr") or result.get("stdout") or "playbook exited non-zero"
            logger.error(
                "Huawei command failed: get_ports on device=%s — %s",
                device.name, error,
            )
            raise RuntimeError(
                f"Cannot determine port state on device '{device.name}': {error}"
            )

        stdouts = result.get("stdouts") or []
        if not stdouts:
            logger.error(
                "Huawei get_ports on device=%s returned no command outputs",
                device.name,
            )
            raise RuntimeError(
                f"Cannot determine port state on device '{device.name}': "
                "no command outputs returned by playbook"
            )

        brief = stdouts[_BRIEF_INDEX] if len(stdouts) > _BRIEF_INDEX else ""
        description = stdouts[_DESCRIPTION_INDEX] if len(stdouts) > _DESCRIPTION_INDEX else ""
        port_vlan = stdouts[_PORT_VLAN_INDEX] if len(stdouts) > _PORT_VLAN_INDEX else ""

        logger.debug(
            "Huawei get_ports raw lengths on device=%s: brief=%d desc=%d portvlan=%d",
            device.name, len(brief), len(description), len(port_vlan),
        )

        try:
            ports = parse_vrp_ports(brief, description, port_vlan)
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [list_ports parse device=%s]: %s\n%s",
                device.name, str(exc), traceback.format_exc(),
            )
            raise RuntimeError(
                f"Cannot determine port state on device '{device.name}': {exc}"
            ) from exc

        logger.info(
            "Huawei port parser returned %d interfaces on device=%s",
            len(ports), device.name,
        )
        return ports
