from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING

from app.services import ansible_service
from app.services.parsers.cisco_port_parser import parse_ios_ports
from app.services.vendors.base import VendorDriver

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.port import Puerto
    from app.models.vlan import VLAN

logger = logging.getLogger(__name__)

_NETWORK_OS = "ios"
_CONNECTION = "network_cli"

_PLAYBOOK_CREATE = "vendors/cisco/create_vlan.yml"
_PLAYBOOK_DELETE = "vendors/cisco/delete_vlan.yml"
_PLAYBOOK_GET = "vendors/cisco/get_vlans.yml"
_PLAYBOOK_UPDATE = "vendors/cisco/update_vlan.yml"

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


def _normalize_result(raw: dict) -> dict:
    """Attach a ``success`` boolean to an Ansible result dict.

    Callers that already read ``rc`` / ``stdout`` / ``stderr`` are unaffected;
    the new key is purely additive and satisfies the ``VendorDriver``
    mutation-result contract.
    """
    return {**raw, "success": raw.get("rc", 1) == 0}


def _build_inventory(device: Device, password: str) -> dict:
    return ansible_service.build_inventory(
        device.name, device.host, device.username, password,
        network_os=_NETWORK_OS, connection=_CONNECTION,
    )


class CiscoVendor(VendorDriver):
    """Vendor driver for Cisco IOS / IOS-XE devices — VLAN + port operations
    fused into one class (FINAL_ARCHITECTURE.md §1.6; ``Device.driver`` is a
    single property, ver `docs/migracion-final-architecture/FASE_1.md` A2).

    Selects Cisco-specific Ansible playbooks, executes them via
    ``ansible_service``, and normalizes all outputs to the ``VendorDriver``
    contract.

    Normalized API
    --------------
    Mutation methods return:
        ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``

    Query methods return:
        ``list_vlans`` → ``list[VLAN]``
        ``get_vlan``   → ``VLAN | None``  (inherited default via list_vlans)
        ``get_vlans``  → same as ``list_vlans`` (backward-compat alias)

    Notes
    -----
    ``save_config`` is not applicable to IOS — the running config is written
    immediately.  Calling it raises ``NotImplementedError``.

    Port mutation coverage is partial (Step 3.1): ``configure_port``,
    ``shutdown_port`` and ``enable_port`` are not yet implemented for Cisco —
    calling them raises ``NotImplementedError`` with a Cisco-specific message
    pointing at the individual ``set_port_*`` methods to use instead.
    """

    # ── VLAN mutation operations ──────────────────────────────────────────────

    def create_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        """Provision *vlan_id* with label *name* on *device*.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """
        logger.info("CiscoVendor: create VLAN %s on device=%s", vlan_id, device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_CREATE,
                extravars={"vlan_id": vlan_id, "vlan_name": name, "device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = _normalize_result(result)
            if normalized["success"]:
                logger.info(
                    "CiscoVendor: create VLAN %s on device=%s — OK",
                    vlan_id, device.name,
                )
            else:
                logger.error(
                    "CiscoVendor: create VLAN %s on device=%s — FAILED: %s",
                    vlan_id, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL CISCO TRACEBACK [create_vlan vlan_id=%s device=%s]: %s\n%s",
                vlan_id, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def delete_vlan(self, vlan_id: int, device: Device, password: str) -> dict:
        """Remove *vlan_id* from *device*.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """
        logger.info("CiscoVendor: delete VLAN %s on device=%s", vlan_id, device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_DELETE,
                extravars={"vlan_id": vlan_id, "device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = _normalize_result(result)
            if normalized["success"]:
                logger.info(
                    "CiscoVendor: delete VLAN %s on device=%s — OK",
                    vlan_id, device.name,
                )
            else:
                logger.error(
                    "CiscoVendor: delete VLAN %s on device=%s — FAILED: %s",
                    vlan_id, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL CISCO TRACEBACK [delete_vlan vlan_id=%s device=%s]: %s\n%s",
                vlan_id, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def update_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        """Rename / update the description of *vlan_id* on *device*.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """
        logger.info("CiscoVendor: update VLAN %s on device=%s", vlan_id, device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_UPDATE,
                extravars={"vlan_id": vlan_id, "description": name, "device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = _normalize_result(result)
            if normalized["success"]:
                logger.info(
                    "CiscoVendor: update VLAN %s on device=%s — OK",
                    vlan_id, device.name,
                )
            else:
                logger.error(
                    "CiscoVendor: update VLAN %s on device=%s — FAILED: %s",
                    vlan_id, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL CISCO TRACEBACK [update_vlan vlan_id=%s device=%s]: %s\n%s",
                vlan_id, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def save_config(self, device: Device, password: str) -> dict:
        """Not supported — Cisco IOS commits changes to the running config immediately.

        Raises
        ------
        NotImplementedError
            Always.  Use ``write memory`` explicitly if non-volatile persistence
            is required, or implement a subclass that calls the appropriate
            playbook.
        """
        raise NotImplementedError(
            f"save_config is not supported for Cisco IOS device '{device.name}'. "
            "Cisco IOS writes changes to the running config automatically."
        )

    # ── VLAN query operations ─────────────────────────────────────────────────

    def list_vlans(self, device: Device, password: str) -> list[VLAN]:
        """Return all user VLANs configured on *device*.

        Runs the ``get_vlans`` playbook, strips ANSI escape codes from the
        IOS ``show vlan brief`` output, and delegates parsing to
        ``parse_vlan_brief``.

        Parameters
        ----------
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        list[VLAN]
            Normalized VLAN entries, excluding IOS-internal VLANs 1 and
            1002–1005.

        Raises
        ------
        RuntimeError
            If the playbook returns a non-zero exit code.
        """
        logger.info("CiscoVendor: list VLANs on device=%s", device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_GET,
                extravars={"device": device.name},
                inventory=_build_inventory(device, password),
            )
            if result["rc"] != 0:
                error = result.get("stderr") or result.get("stdout") or "get_vlans playbook failed"
                logger.error(
                    "CiscoVendor: list VLANs on device=%s — FAILED: %s",
                    device.name, error,
                )
                raise RuntimeError(error)
            from app.services.parsers.vlan_parser import parse_vlan_brief
            vlans = parse_vlan_brief(result["stdout"])
            logger.info(
                "CiscoVendor: list VLANs on device=%s — returned %d VLANs",
                device.name, len(vlans),
            )
            return vlans
        except RuntimeError:
            raise
        except Exception as exc:
            logger.exception(
                "FULL CISCO TRACEBACK [list_vlans device=%s]: %s\n%s",
                device.name, str(exc), traceback.format_exc(),
            )
            raise

    def get_vlans(self, device: Device, password: str) -> list[VLAN]:
        """Backward-compatible alias for ``list_vlans()``.

        All existing callers continue to work without modification.
        New code should call ``list_vlans()`` directly.
        """
        return self.list_vlans(device, password)

    # ── Port query operation ──────────────────────────────────────────────────

    def list_ports(self, device: Device, password: str) -> list[Puerto]:
        """Return all physical switchports on *device* as ``Puerto`` objects.

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
        list[Puerto]
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

    # ── Port mutation operations ──────────────────────────────────────────────

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

    # ── Step 3.1 composite / semantic stubs ───────────────────────────────────

    def configure_port(
        self,
        config: Puerto,
        device: Device,
        password: str,
    ) -> dict:
        """Composite port configuration — Step 3.1 stub (not yet implemented)."""
        raise NotImplementedError(
            "CiscoVendor.configure_port is not yet implemented — "
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
            "CiscoVendor.shutdown_port is not yet implemented — "
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
            "CiscoVendor.enable_port is not yet implemented — "
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
        vlan_str = self._compress_vlans_cisco(sorted(set(vlan_list)))
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

    # ── VLAN list compression (Fase 2, A2 — movida desde validators/port_validator.py,
    # no es validación, es formato de CLI, específico de este vendor) ────────────

    @staticmethod
    def _compress_to_ranges(vlans: list[int]) -> list[tuple[int, int]]:
        """Collapse *vlans* into (start, end) range tuples."""
        if not vlans:
            return []
        sv = sorted(set(vlans))
        ranges: list[tuple[int, int]] = []
        start = sv[0]
        prev = sv[0]
        for v in sv[1:]:
            if v == prev + 1:
                prev = v
            else:
                ranges.append((start, prev))
                start = v
                prev = v
        ranges.append((start, prev))
        return ranges

    def _compress_vlans_cisco(self, vlans: list[int]) -> str:
        """Format a VLAN list into the Cisco IOS trunk-allowed syntax.

        Example: [10, 11, 12, 20] → ``"10-12,20"``
        """
        parts = []
        for s, e in self._compress_to_ranges(vlans):
            parts.append(f"{s}-{e}" if s != e else str(s))
        return ",".join(parts)
