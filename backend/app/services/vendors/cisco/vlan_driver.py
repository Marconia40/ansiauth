from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING

from app.services import ansible_service
from app.services.vendors.base import BaseVendorDriver

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.vlan import VLANInfo

logger = logging.getLogger(__name__)

_NETWORK_OS = "ios"
_CONNECTION = "network_cli"

_PLAYBOOK_CREATE = "vendors/cisco/create_vlan.yml"
_PLAYBOOK_DELETE = "vendors/cisco/delete_vlan.yml"
_PLAYBOOK_GET = "vendors/cisco/get_vlans.yml"
_PLAYBOOK_UPDATE = "vendors/cisco/update_vlan.yml"


def _normalize_result(raw: dict) -> dict:
    """Attach a ``success`` boolean to an Ansible result dict.

    Callers that already read ``rc`` / ``stdout`` / ``stderr`` are unaffected;
    the new key is purely additive and satisfies the ``BaseVendorDriver``
    mutation-result contract.
    """
    return {**raw, "success": raw.get("rc", 1) == 0}


def _build_inventory(device: Device, password: str) -> dict:
    return ansible_service.build_inventory(
        device.name, device.host, device.username, password,
        network_os=_NETWORK_OS, connection=_CONNECTION,
    )


class CiscoVlanDriver(BaseVendorDriver):
    """Vendor driver for Cisco IOS / IOS-XE devices.

    Selects Cisco-specific Ansible playbooks, executes them via
    ``ansible_service``, and normalizes all outputs to the
    ``BaseVendorDriver`` contract.

    Normalized API
    --------------
    Mutation methods return:
        ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``

    Query methods return:
        ``list_vlans`` → ``list[VLANInfo]``
        ``get_vlan``   → ``VLANInfo | None``  (inherited default via list_vlans)
        ``get_vlans``  → same as ``list_vlans`` (backward-compat alias)

    Notes
    -----
    ``save_config`` is not applicable to IOS — the running config is written
    immediately.  Calling it raises ``NotImplementedError``.
    """

    # ── Mutation operations ───────────────────────────────────────────────────

    def create_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        """Provision *vlan_id* with label *name* on *device*.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """
        logger.info("CiscoVlanDriver: create VLAN %s on device=%s", vlan_id, device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_CREATE,
                extravars={"vlan_id": vlan_id, "vlan_name": name, "device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = _normalize_result(result)
            if normalized["success"]:
                logger.info(
                    "CiscoVlanDriver: create VLAN %s on device=%s — OK",
                    vlan_id, device.name,
                )
            else:
                logger.error(
                    "CiscoVlanDriver: create VLAN %s on device=%s — FAILED: %s",
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
        logger.info("CiscoVlanDriver: delete VLAN %s on device=%s", vlan_id, device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_DELETE,
                extravars={"vlan_id": vlan_id, "device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = _normalize_result(result)
            if normalized["success"]:
                logger.info(
                    "CiscoVlanDriver: delete VLAN %s on device=%s — OK",
                    vlan_id, device.name,
                )
            else:
                logger.error(
                    "CiscoVlanDriver: delete VLAN %s on device=%s — FAILED: %s",
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
        logger.info("CiscoVlanDriver: update VLAN %s on device=%s", vlan_id, device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_UPDATE,
                extravars={"vlan_id": vlan_id, "description": name, "device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = _normalize_result(result)
            if normalized["success"]:
                logger.info(
                    "CiscoVlanDriver: update VLAN %s on device=%s — OK",
                    vlan_id, device.name,
                )
            else:
                logger.error(
                    "CiscoVlanDriver: update VLAN %s on device=%s — FAILED: %s",
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

    # ── Query operations ──────────────────────────────────────────────────────

    def list_vlans(self, device: Device, password: str) -> list[VLANInfo]:
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
        list[VLANInfo]
            Normalized VLAN entries, excluding IOS-internal VLANs 1 and
            1002–1005.

        Raises
        ------
        RuntimeError
            If the playbook returns a non-zero exit code.
        """
        logger.info("CiscoVlanDriver: list VLANs on device=%s", device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_GET,
                extravars={"device": device.name},
                inventory=_build_inventory(device, password),
            )
            if result["rc"] != 0:
                error = result.get("stderr") or result.get("stdout") or "get_vlans playbook failed"
                logger.error(
                    "CiscoVlanDriver: list VLANs on device=%s — FAILED: %s",
                    device.name, error,
                )
                raise RuntimeError(error)
            from app.services.parsers.vlan_parser import parse_vlan_brief
            vlans = parse_vlan_brief(result["stdout"])
            logger.info(
                "CiscoVlanDriver: list VLANs on device=%s — returned %d VLANs",
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

    def get_vlans(self, device: Device, password: str) -> list[VLANInfo]:
        """Backward-compatible alias for ``list_vlans()``.

        All existing callers continue to work without modification.
        New code should call ``list_vlans()`` directly.
        """
        return self.list_vlans(device, password)
