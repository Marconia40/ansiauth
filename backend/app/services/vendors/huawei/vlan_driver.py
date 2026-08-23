from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING

from app.services import ansible_service
from app.services.vendors.base import BaseVendorDriver

if TYPE_CHECKING:
    from app.models.vlan import VLAN

logger = logging.getLogger(__name__)

_NETWORK_OS = "community.network.ce"
_CONNECTION = "network_cli"

_PLAYBOOK_CREATE = "vendors/huawei/create_vlan.yml"
_PLAYBOOK_DELETE = "vendors/huawei/delete_vlan.yml"
_PLAYBOOK_GET = "vendors/huawei/get_vlans.yml"
_PLAYBOOK_UPDATE = "vendors/huawei/update_vlan.yml"
_PLAYBOOK_SAVE = "vendors/huawei/save_config.yml"


def _normalize_result(raw: dict) -> dict:
    """Attach a ``success`` boolean to an Ansible result dict.

    Callers that already read ``rc`` / ``stdout`` / ``stderr`` are unaffected;
    the new key is purely additive.
    """
    return {**raw, "success": raw.get("rc", 1) == 0}


def _build_inventory(device, password: str) -> dict:
    return ansible_service.build_inventory(
        device.name, device.host, device.username, password,
        network_os=_NETWORK_OS, connection=_CONNECTION,
    )


class HuaweiVlanDriver(BaseVendorDriver):
    """Vendor driver for Huawei VRP devices.

    Uses ansible.netcommon.cli_command / cli_config modules over network_cli
    with community.network.ce as the terminal plugin.  This avoids the
    ce_command JSON-decode failure that occurs when VRP devices return plain
    CLI text rather than structured output.

    Normalized API
    --------------
    Mutation methods return:
        {"rc": int, "stdout": str, "stderr": str, "success": bool}

    Query methods return:
        list_vlans → list[{"vlan_id": int, "name": str}]
        get_vlan   → {"vlan_id": int, "name": str} | None
        get_vlans  → same as list_vlans (backward-compat alias)
    """

    # ── Mutation operations ───────────────────────────────────────────────────

    def create_vlan(self, vlan_id: int, name: str, device, password: str) -> dict:
        """Create *vlan_id* with label *name* on *device*.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """
        logger.info(
            "Executing Huawei command: create VLAN %s on device=%s",
            vlan_id, device.name,
        )
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_CREATE,
                extravars={"vlan_id": vlan_id, "vlan_name": name, "device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = _normalize_result(result)
            if normalized["success"]:
                logger.info(
                    "Huawei command completed successfully: create VLAN %s on device=%s",
                    vlan_id, device.name,
                )
            else:
                logger.error(
                    "Huawei command failed: create VLAN %s on device=%s — %s",
                    vlan_id, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [create_vlan vlan_id=%s device=%s]: %s\n%s",
                vlan_id, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def delete_vlan(self, vlan_id: int, device, password: str) -> dict:
        """Remove *vlan_id* from *device*.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """
        logger.info(
            "Executing Huawei command: delete VLAN %s on device=%s",
            vlan_id, device.name,
        )
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_DELETE,
                extravars={"vlan_id": vlan_id, "device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = _normalize_result(result)
            if normalized["success"]:
                logger.info(
                    "Huawei command completed successfully: delete VLAN %s on device=%s",
                    vlan_id, device.name,
                )
            else:
                logger.error(
                    "Huawei command failed: delete VLAN %s on device=%s — %s",
                    vlan_id, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [delete_vlan vlan_id=%s device=%s]: %s\n%s",
                vlan_id, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def update_vlan(self, vlan_id: int, name: str, device, password: str) -> dict:
        """Rename / update the description of *vlan_id* on *device*.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """
        logger.info(
            "Executing Huawei command: update VLAN %s on device=%s",
            vlan_id, device.name,
        )
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_UPDATE,
                extravars={"vlan_id": vlan_id, "description": name, "device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = _normalize_result(result)
            if normalized["success"]:
                logger.info(
                    "Huawei command completed successfully: update VLAN %s on device=%s",
                    vlan_id, device.name,
                )
            else:
                logger.error(
                    "Huawei command failed: update VLAN %s on device=%s — %s",
                    vlan_id, device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [update_vlan vlan_id=%s device=%s]: %s\n%s",
                vlan_id, device.name, str(exc), traceback.format_exc(),
            )
            raise

    def save_config(self, device, password: str) -> dict:
        """Persist the running configuration on *device* via ``save force``.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """
        logger.info("Executing Huawei command: save force on device=%s", device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_SAVE,
                extravars={"device": device.name},
                inventory=_build_inventory(device, password),
            )
            normalized = _normalize_result(result)
            if normalized["success"]:
                logger.info(
                    "Huawei command completed successfully: save force on device=%s",
                    device.name,
                )
            else:
                logger.error(
                    "Huawei command failed: save force on device=%s — %s",
                    device.name,
                    result.get("stderr") or result.get("stdout"),
                )
            return normalized
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [save_config device=%s]: %s\n%s",
                device.name, str(exc), traceback.format_exc(),
            )
            raise

    # ── Query operations ──────────────────────────────────────────────────────

    def list_vlans(self, device, password: str) -> list[VLAN]:
        """Return all VLANs configured on *device*, excluding internal VLANs.

        Runs the ``get_vlans`` playbook, strips ANSI escape codes from the
        output, and delegates parsing to ``parse_vrp_vlan_display`` which
        handles both VRP tabular and block output formats.

        Parameters
        ----------
        device:
            ORM device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        list[VLAN]
            Normalized VLAN entries.

        Raises
        ------
        RuntimeError
            If the playbook fails, returns empty output, or the output
            cannot be parsed.
        """
        logger.info("Executing Huawei command: display vlan on device=%s", device.name)
        try:
            result = ansible_service.run_playbook(
                playbook=_PLAYBOOK_GET,
                extravars={"device": device.name},
                inventory=_build_inventory(device, password),
            )
            if result["rc"] != 0:
                error = result.get("stderr") or result.get("stdout") or "playbook exited non-zero"
                logger.error(
                    "Huawei command failed: display vlan on device=%s — %s",
                    device.name, error,
                )
                raise RuntimeError(
                    f"Cannot determine VLAN state on device '{device.name}': {error}"
                )

            stdout = result.get("stdout", "")
            if not stdout or not stdout.strip():
                logger.error(
                    "Huawei command failed: display vlan on device=%s returned empty output",
                    device.name,
                )
                raise RuntimeError(
                    f"Cannot determine VLAN state on device '{device.name}': "
                    "empty output from display vlan"
                )

            logger.debug(
                "Huawei display vlan raw output (%d chars) on device=%s: %.500s",
                len(stdout), device.name, stdout,
            )

            try:
                from app.services.parsers.vlan_parser import parse_vrp_vlan_display
                vlans = parse_vrp_vlan_display(stdout)
                logger.info(
                    "Huawei VLAN parse returned %d VLANs on device=%s",
                    len(vlans), device.name,
                )
                return vlans
            except Exception as exc:
                logger.exception(
                    "FULL HUAWEI TRACEBACK [list_vlans parse device=%s]: %s\n%s",
                    device.name, str(exc), traceback.format_exc(),
                )
                raise RuntimeError(
                    f"Cannot determine VLAN state on device '{device.name}': {exc}"
                ) from exc

        except RuntimeError:
            raise
        except Exception as exc:
            logger.exception(
                "FULL HUAWEI TRACEBACK [list_vlans device=%s]: %s\n%s",
                device.name, str(exc), traceback.format_exc(),
            )
            raise

    def get_vlans(self, device, password: str) -> list[VLAN]:
        """Backward-compatible alias for ``list_vlans()``.

        All existing callers (vlan_service, tests) continue to work without
        modification.  New code should call ``list_vlans()`` directly.
        """
        return self.list_vlans(device, password)
