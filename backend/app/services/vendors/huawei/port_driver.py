from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING

from app.services import ansible_service
from app.services.parsers.port_parser import parse_vrp_ports
from app.services.vendors.port_driver_base import BasePortDriver

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.port import PortConfigRequest, PortConfigResult, PortInfo

logger = logging.getLogger(__name__)

_NETWORK_OS = "community.network.ce"
_CONNECTION = "network_cli"

_PLAYBOOK_GET_PORTS = "vendors/huawei/get_ports.yml"
_PLAYBOOK_UPDATE_DESCRIPTION = "vendors/huawei/update_port_description.yml"
_PLAYBOOK_SET_ADMIN_STATE = "vendors/huawei/set_port_admin_state.yml"
_PLAYBOOK_SET_ACCESS_VLAN = "vendors/huawei/set_access_vlan.yml"
_PLAYBOOK_SET_TRUNK_VLANS = "vendors/huawei/set_trunk_allowed_vlans.yml"
_PLAYBOOK_CONFIGURE_PORT = "vendors/huawei/configure_port.yml"
_PLAYBOOK_SHUTDOWN_PORT = "vendors/huawei/shutdown_port.yml"
_PLAYBOOK_ENABLE_PORT = "vendors/huawei/enable_port.yml"

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

    # ── Mutation operations ──────────────────────────────────────────────────

    def update_port_description(
        self,
        interface: str,
        description: str,
        device: Device,
        password: str,
    ) -> dict:
        """Set the port description on a Huawei VRP device.

        Runs the ``update_port_description`` playbook with the description
        text (or its emptiness flag) as Ansible vars.  Returns the
        normalized Ansible result with the additive ``success`` key.

        Empty / whitespace-only ``description`` clears the description via
        ``undo description`` — chosen so an operator who wipes the field
        in the UI gets a clean state on the device, not a literal empty
        string.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        is_empty = not bool(description and description.strip())
        logger.info(
            "Huawei: update description on interface=%s device=%s (clear=%s)",
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
                    "Huawei: update description OK on interface=%s device=%s",
                    interface, device.name,
                )
            else:
                logger.error(
                    "Huawei: update description FAILED on interface=%s device=%s — %s",
                    interface, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [update_port_description interface=%s device=%s]: %s\n%s",
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
        """Administratively enable / disable *interface* on a Huawei VRP device.

        Runs the ``set_port_admin_state`` playbook with the desired state as
        an Ansible var; the playbook applies ``undo shutdown`` or ``shutdown``
        inside the interface view and commits.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        logger.info(
            "Huawei: set admin state on interface=%s device=%s enabled=%s",
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
                    "Huawei: set admin state OK on interface=%s device=%s enabled=%s",
                    interface, device.name, enabled,
                )
            else:
                logger.error(
                    "Huawei: set admin state FAILED on interface=%s device=%s enabled=%s — %s",
                    interface, device.name, enabled,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [set_port_admin_state interface=%s device=%s]: %s\n%s",
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
        """Set the access VLAN of *interface* on a Huawei VRP device.

        Runs ``port default vlan {{ vlan_id }}`` inside the interface view.
        The orchestration layer must guarantee the port is already in
        access mode before this is called.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        logger.info(
            "Huawei: set access VLAN on interface=%s device=%s vlan_id=%d",
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
                    "Huawei: set access VLAN OK on interface=%s device=%s vlan_id=%d",
                    interface, device.name, vlan_id,
                )
            else:
                logger.error(
                    "Huawei: set access VLAN FAILED on interface=%s device=%s vlan_id=%d — %s",
                    interface, device.name, vlan_id,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [set_port_access_vlan interface=%s device=%s]: %s\n%s",
                interface, device.name, str(exc), traceback.format_exc(),
            )
            raise

    # ── Step 3.2 composite / semantic implementations ────────────────────────────

    def configure_port(
        self,
        config: PortConfigRequest,
        device: Device,
        password: str,
    ) -> PortConfigResult:
        """Apply a composite set of port mutations on a Huawei VRP device.

        All requested fields are applied in a single candidate-config session
        and committed atomically via ``commit``.  Only the sections whose
        corresponding boolean flag is ``True`` are emitted — no mutation
        touches fields that were not set in *config*.

        Field application order (VRP constraint):
            1. ``port link-type`` (mode) — must precede VLAN commands.
            2. VLAN assignment (access or trunk, never both).
            3. ``description`` / ``undo description``.
            4. ``shutdown`` / ``undo shutdown`` (admin state).
            5. ``commit`` + ``quit``.

        Returns
        -------
        PortConfigResult
            ``success=True`` and ``changed=True`` when ``rc == 0``.
            ``success=False`` and ``changed=False`` on playbook failure.
            ``rollback_performed`` is always ``None`` — the orchestration layer
            sets this field after deciding whether to trigger rollback.
        """
        import time
        from app.models.port import PortConfigResult as _PCR
        from app.validators.port_validator import compress_vlans_huawei

        start = time.time()

        configure_description = config.description is not None
        description = config.description if config.description is not None else ""
        description_is_empty = not bool(description and description.strip())

        configure_admin = config.admin_enabled is not None
        admin_enabled = bool(config.admin_enabled) if config.admin_enabled is not None else False

        configure_mode = config.mode is not None
        mode = config.mode or ""

        configure_access_vlan = config.access_vlan is not None
        access_vlan = config.access_vlan if config.access_vlan is not None else 0

        configure_trunk_vlans = config.allowed_vlans is not None
        vlan_list = (
            compress_vlans_huawei(sorted(set(config.allowed_vlans)))
            if config.allowed_vlans
            else ""
        )

        logger.info(
            "Huawei: configure_port on interface=%s device=%s fields=%s",
            config.interface, device.name, config.mutation_fields,
        )

        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_CONFIGURE_PORT,
                extravars={
                    "interface": config.interface,
                    "device": device.name,
                    "configure_mode": configure_mode,
                    "mode": mode,
                    "configure_access_vlan": configure_access_vlan,
                    "access_vlan": access_vlan,
                    "configure_trunk_vlans": configure_trunk_vlans,
                    "vlan_list": vlan_list,
                    "configure_description": configure_description,
                    "description": description,
                    "description_is_empty": description_is_empty,
                    "configure_admin": configure_admin,
                    "admin_enabled": admin_enabled,
                },
                inventory=_build_inventory(device, password),
            )
            elapsed_ms = (time.time() - start) * 1000
            success = result.get("rc", 1) == 0
            if success:
                logger.info(
                    "Huawei: configure_port OK on interface=%s device=%s in %.0fms fields=%s",
                    config.interface, device.name, elapsed_ms, config.mutation_fields,
                )
            else:
                logger.error(
                    "Huawei: configure_port FAILED on interface=%s device=%s — %s",
                    config.interface, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return _PCR(
                success=success,
                changed=success,
                interface=config.interface,
                vendor="huawei_vrp",
                execution_time_ms=round(elapsed_ms, 1),
                rollback_performed=None,
                warnings=None,
            )
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [configure_port interface=%s device=%s]: %s\n%s",
                config.interface, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def shutdown_port(
        self,
        interface: str,
        device: Device,
        password: str,
    ) -> dict:
        """Administratively disable *interface* on a Huawei VRP device.

        Runs ``shutdown`` inside the interface view and commits atomically.
        Intent-named counterpart to ``enable_port`` — produces self-describing
        Ansible run logs without requiring callers to decode a boolean flag.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        logger.info(
            "Huawei: shutdown_port on interface=%s device=%s", interface, device.name
        )
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_SHUTDOWN_PORT,
                extravars={"interface": interface, "device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = {**result, "success": result.get("rc", 1) == 0}
            if normalized["success"]:
                logger.info(
                    "Huawei: shutdown_port OK on interface=%s device=%s",
                    interface, device.name,
                )
            else:
                logger.error(
                    "Huawei: shutdown_port FAILED on interface=%s device=%s — %s",
                    interface, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [shutdown_port interface=%s device=%s]: %s\n%s",
                interface, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def enable_port(
        self,
        interface: str,
        device: Device,
        password: str,
    ) -> dict:
        """Administratively enable *interface* on a Huawei VRP device.

        Runs ``undo shutdown`` inside the interface view and commits atomically.
        Intent-named counterpart to ``shutdown_port``.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        logger.info(
            "Huawei: enable_port on interface=%s device=%s", interface, device.name
        )
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_ENABLE_PORT,
                extravars={"interface": interface, "device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = {**result, "success": result.get("rc", 1) == 0}
            if normalized["success"]:
                logger.info(
                    "Huawei: enable_port OK on interface=%s device=%s",
                    interface, device.name,
                )
            else:
                logger.error(
                    "Huawei: enable_port FAILED on interface=%s device=%s — %s",
                    interface, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [enable_port interface=%s device=%s]: %s\n%s",
                interface, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def set_trunk_allowed_vlans(
        self,
        interface: str,
        vlan_list: list[int],
        device: Device,
        password: str,
    ) -> dict:
        """Set the trunk allowed-VLAN list of *interface* on a Huawei VRP device.

        Runs ``undo port trunk allow-pass vlan all`` then
        ``port trunk allow-pass vlan <list>`` inside the interface view,
        followed by ``commit``.  Both commands are in the candidate config and
        committed atomically so no transient blackout occurs.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        from app.validators.port_validator import compress_vlans_huawei
        vlan_str = compress_vlans_huawei(sorted(set(vlan_list)))
        logger.info(
            "Huawei: set trunk VLANs on interface=%s device=%s vlans=%s",
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
                    "Huawei: set trunk VLANs OK on interface=%s device=%s vlans=%s",
                    interface, device.name, vlan_str,
                )
            else:
                logger.error(
                    "Huawei: set trunk VLANs FAILED on interface=%s device=%s vlans=%s — %s",
                    interface, device.name, vlan_str,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [set_trunk_allowed_vlans interface=%s device=%s]: %s\n%s",
                interface, device.name, str(exc), traceback.format_exc(),
            )
            raise
