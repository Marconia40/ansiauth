from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.parsers.cisco_port_parser import parse_ios_ports
from app.services.vendors.base import VendorDriver

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.port import Puerto
    from app.models.vlan import VLAN

_PLAYBOOK = "vendors/cisco/run.yml"

# The Cisco get_ports read issues 3 commands in this order.  If that order
# changes, these indices must be updated alongside it.
_STATUS_INDEX = 0
_DESCRIPTION_INDEX = 1
_SWITCHPORT_INDEX = 2


class CiscoVendor(VendorDriver):
    """Vendor driver for Cisco IOS / IOS-XE devices — VLAN + port operations
    fused into one class (FINAL_ARCHITECTURE.md §1.6; ``Device.driver`` is a
    single property, ver `docs/migracion-final-architecture/FASE_1.md` A2).

    All operations run through a single playbook, ``vendors/cisco/run.yml``,
    with 2 modes selected by which extravar is present:
    ``cisco.ios.ios_config`` (``lines``/``parents``, for mutations) or
    ``cisco.ios.ios_command`` (``commands``, for reads *and* ``save_config()``
    — ``write`` is an exec-mode command on IOS, same register as ``show``,
    not a config line). ``VendorDriver._aplicar()``/``._leer()`` own the
    run-playbook/normalize/log boilerplate; this class only builds each
    operation's command content.

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
    ``save_config()`` is a real, explicit operation (``write``) — it is
    never called automatically after a mutation; nothing in the app calls
    it today for either vendor, and mutations here do not auto-persist to
    startup-config (unlike the old per-operation playbooks' ``save_when:
    always``). Persisting is a deliberate, separate action.
    """

    _NETWORK_OS = "ios"
    _PLAYBOOK = _PLAYBOOK

    # ── VLAN mutation operations ──────────────────────────────────────────────

    def create_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        return self._aplicar(
            {"parents": f"vlan {vlan_id}", "lines": [f"name {name}"]},
            device, password, op_label=f"create VLAN {vlan_id}",
        )

    def delete_vlan(self, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar(
            {"lines": [f"no vlan {vlan_id}"]},
            device, password, op_label=f"delete VLAN {vlan_id}",
        )

    def update_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        return self._aplicar(
            {"parents": f"vlan {vlan_id}", "lines": [f"name {name}"]},
            device, password, op_label=f"update VLAN {vlan_id}",
        )

    def save_config(self, device: Device, password: str) -> dict:
        """Persist the running configuration via ``write`` (exec-mode
        command, equivalent to ``copy running-config startup-config``).

        Explicit operation only — never invoked automatically by any
        mutation method in this class.
        """
        return self._aplicar({"commands": ["write"]}, device, password, op_label="save config")

    # ── VLAN query operations ─────────────────────────────────────────────────

    def list_vlans(self, device: Device, password: str) -> list[VLAN]:
        stdouts = self._leer(["show vlan brief"], device, password)
        from app.services.parsers.vlan_parser import parse_vlan_brief
        vlans = parse_vlan_brief(stdouts[0])
        return vlans

    def get_vlans(self, device: Device, password: str) -> list[VLAN]:
        """Backward-compatible alias for ``list_vlans()``."""
        return self.list_vlans(device, password)

    # ── Port query operation ──────────────────────────────────────────────────

    def list_ports(self, device: Device, password: str) -> list[Puerto]:
        stdouts = self._leer(
            ["show interfaces status", "show interfaces description", "show interfaces switchport"],
            device, password,
        )
        status = stdouts[_STATUS_INDEX] if len(stdouts) > _STATUS_INDEX else ""
        description = stdouts[_DESCRIPTION_INDEX] if len(stdouts) > _DESCRIPTION_INDEX else ""
        switchport = stdouts[_SWITCHPORT_INDEX] if len(stdouts) > _SWITCHPORT_INDEX else ""
        try:
            ports = parse_ios_ports(status, description, switchport)
        except Exception as exc:
            raise RuntimeError(f"Cannot determine port state on device '{device.name}': {exc}") from exc
        return ports

    # ── Port mutation operations ──────────────────────────────────────────────

    def update_port_description(self, interface: str, description: str, device: Device, password: str) -> dict:
        is_empty = not bool(description and description.strip())
        line = "no description" if is_empty else f"description {description}"
        return self._aplicar(
            {"parents": f"interface {interface}", "lines": [line]},
            device, password, op_label="update port description",
        )

    def set_port_admin_state(self, interface: str, enabled: bool, device: Device, password: str) -> dict:
        line = "no shutdown" if enabled else "shutdown"
        return self._aplicar(
            {"parents": f"interface {interface}", "lines": [line]},
            device, password, op_label=f"set admin state enabled={enabled}",
        )

    def set_port_access_vlan(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar(
            {"parents": f"interface {interface}", "lines": [f"switchport access vlan {vlan_id}"]},
            device, password, op_label="set access VLAN",
        )

    def set_trunk_pvid_vlan(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar(
            {"parents": f"interface {interface}", "lines": [f"switchport trunk native vlan {vlan_id}"]},
            device, password, op_label="set trunk native VLAN",
        )

    def set_trunk_allowed_vlans(self, interface: str, vlan_list: list[int], device: Device, password: str) -> dict:
        vlan_str = self._compress_vlans_cisco(sorted(set(vlan_list)))
        return self._aplicar(
            {"parents": f"interface {interface}", "lines": [f"switchport trunk allowed vlan {vlan_str}"]},
            device, password, op_label="set trunk allowed VLANs",
        )

    def set_access_mode(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar(
            {
                "parents": f"interface {interface}",
                "lines": ["switchport mode access", f"switchport access vlan {vlan_id}"],
            },
            device, password, op_label="set access mode",
        )

    def set_trunk_mode(
        self, interface: str, native_vlan: int, vlan_list: list[int], device: Device, password: str,
    ) -> dict:
        vlan_str = self._compress_vlans_cisco(sorted(set(vlan_list)))
        return self._aplicar(
            {
                "parents": f"interface {interface}",
                "lines": [
                    "switchport mode trunk",
                    f"switchport trunk native vlan {native_vlan}",
                    f"switchport trunk allowed vlan {vlan_str}",
                ],
            },
            device, password, op_label="set trunk mode",
        )

    # ── VLAN list compression (Fase 2, A2 — movida desde validators/port_validator.py,
    # no es validación, es formato de CLI, específico de este vendor) ────────────

    def _compress_vlans_cisco(self, vlans: list[int]) -> str:
        """Format a VLAN list into the Cisco IOS trunk-allowed syntax.

        Example: [10, 11, 12, 20] → ``"10-12,20"``
        """
        parts = []
        for s, e in self._compress_to_ranges(vlans):
            parts.append(f"{s}-{e}" if s != e else str(s))
        return ",".join(parts)
