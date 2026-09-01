from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.parsers.port_parser import parse_vrp_ports
from app.services.vendors.base import VendorDriver

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.port import Puerto
    from app.models.vlan import VLAN

_PLAYBOOK = "vendors/huawei/run.yml"

# The Huawei get_ports read issues 3 commands in this order.
_BRIEF_INDEX = 0
_DESCRIPTION_INDEX = 1
_PORT_VLAN_INDEX = 2


class HuaweiVendor(VendorDriver):
    """Vendor driver for Huawei VRP devices — VLAN + port operations fused
    into one class (FINAL_ARCHITECTURE.md §1.6; ``Device.driver`` is a
    single property, ver `docs/migracion-final-architecture/FASE_1.md` A2).

    All operations run through a single playbook, ``vendors/huawei/run.yml``,
    with 2 modes selected by which extravar is present: a full
    ``system-view``/.../``commit``/``quit`` command block
    (``ansible.netcommon.cli_command``, for mutations *and* ``save_config()``
    — ``save force`` is a bare exec command, fits the same "run this block"
    shape with no ``system-view`` wrapping needed) or a looped list of read
    commands (``commands``). ``VendorDriver._aplicar()``/``._leer()`` own the
    run-playbook/normalize/log boilerplate; this class only builds each
    operation's command block.

    The actual CLI command text lives in ``commands.yaml`` (next to this
    module), not here — real VRP platforms vary in syntax (confirmed
    against real lab hardware: some need ``commit`` after a command, some
    show an interactive ``[Y/N]`` confirmation instead and ``commit``
    breaks them, ``storm-control``/PoE keywords vary by platform family),
    and editing a YAML to add a variant is a lot cheaper than editing this
    class and rebuilding. Each method here only computes the substitution
    values (``vars``) and, for binary operations, which named variant to
    request — ``VendorDriver._aplicar_desde_template()`` does the rest
    (render, execute, retry known-error alternatives).

    Uses ``ansible.netcommon.cli_command`` over ``network_cli`` with
    ``community.network.ce`` as the terminal plugin — avoids the
    ``ce_command`` JSON-decode failure that occurs when VRP devices return
    plain CLI text rather than structured output.

    Normalized API
    --------------
    Mutation methods return:
        {"rc": int, "stdout": str, "stderr": str, "success": bool}

    Query methods return:
        list_vlans → list[VLAN]
        get_vlan   → VLAN | None
        get_vlans  → same as list_vlans (backward-compat alias)
    """

    _NETWORK_OS = "community.network.ce"
    _PLAYBOOK = _PLAYBOOK

    # ── VLAN mutation operations ──────────────────────────────────────────────

    def create_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        return self._aplicar_desde_template(
            "create_vlan", {"vlan_id": vlan_id, "name": name}, device, password,
        )

    def delete_vlan(self, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar_desde_template("delete_vlan", {"vlan_id": vlan_id}, device, password)

    def update_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        return self._aplicar_desde_template(
            "update_vlan", {"vlan_id": vlan_id, "name": name}, device, password,
        )

    def save_config(self, device: Device, password: str) -> dict:
        """Persist the running configuration via ``save force``.

        Explicit operation only — never invoked automatically by any
        mutation method in this class.
        """
        return self._aplicar_desde_template("save_config", {}, device, password)

    # ── VLAN query operations ─────────────────────────────────────────────────

    def list_vlans(self, device: Device, password: str) -> list[VLAN]:
        commands = self._cargar_comandos()["list_vlans"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        from app.services.parsers.vlan_parser import parse_vrp_vlan_display
        return parse_vrp_vlan_display(stdouts[0])

    def get_vlans(self, device: Device, password: str) -> list[VLAN]:
        """Backward-compatible alias for ``list_vlans()``."""
        return self.list_vlans(device, password)

    # ── Port query operation ──────────────────────────────────────────────────

    def list_ports(self, device: Device, password: str) -> list[Puerto]:
        commands = self._cargar_comandos()["list_ports"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        brief = stdouts[_BRIEF_INDEX] if len(stdouts) > _BRIEF_INDEX else ""
        description = stdouts[_DESCRIPTION_INDEX] if len(stdouts) > _DESCRIPTION_INDEX else ""
        port_vlan = stdouts[_PORT_VLAN_INDEX] if len(stdouts) > _PORT_VLAN_INDEX else ""
        try:
            ports = parse_vrp_ports(brief, description, port_vlan)
        except Exception as exc:
            raise RuntimeError(f"Cannot determine port state on device '{device.name}': {exc}") from exc
        return ports

    # ── Port mutation operations ──────────────────────────────────────────────

    def update_port_description(self, interface: str, description: str, device: Device, password: str) -> dict:
        variant = "clear" if self._is_description_empty(description) else "set"
        return self._aplicar_desde_template(
            "update_port_description", {"interface": interface, "description": description},
            device, password, variant=variant,
        )

    def set_port_admin_state(self, interface: str, enabled: bool, device: Device, password: str) -> dict:
        variant = "enabled" if enabled else "disabled"
        return self._aplicar_desde_template(
            "set_port_admin_state", {"interface": interface}, device, password, variant=variant,
        )

    def set_port_access_vlan(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar_desde_template(
            "set_port_access_vlan", {"interface": interface, "vlan_id": vlan_id}, device, password,
        )

    def set_trunk_pvid_vlan(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar_desde_template(
            "set_trunk_pvid_vlan", {"interface": interface, "vlan_id": vlan_id}, device, password,
        )

    def set_trunk_allowed_vlans(self, interface: str, vlan_list: list[int], device: Device, password: str) -> dict:
        vlan_str = self._compress_vlans_huawei(sorted(set(vlan_list)))
        return self._aplicar_desde_template(
            "set_trunk_allowed_vlans", {"interface": interface, "allowed_vlans": vlan_str}, device, password,
        )

    def set_port_poe(self, interface: str, enabled: bool, device: Device, password: str) -> dict:
        variant = "enabled" if enabled else "disabled"
        return self._aplicar_desde_template(
            "set_port_poe", {"interface": interface}, device, password, variant=variant,
        )

    def set_storm_control(
        self, interface: str, enabled: bool, threshold: "float | None", device: Device, password: str,
    ) -> dict:
        variant = "enabled" if enabled else "disabled"
        return self._aplicar_desde_template(
            "set_storm_control", {"interface": interface, "threshold": threshold}, device, password, variant=variant,
        )

    def reset_port(self, interface: str, device: Device, password: str) -> dict:
        return self._aplicar_desde_template("reset_port", {"interface": interface}, device, password)

    # ── Mode-change operations ────────────────────────────────────────────────

    def set_access_mode(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        """Set *interface* to access mode with *vlan_id*, atomically —
        ``port link-type access`` + ``port default vlan``, same single
        candidate-config session as every other mutation on this driver."""
        return self._aplicar_desde_template(
            "set_access_mode", {"interface": interface, "vlan_id": vlan_id}, device, password,
        )

    def set_trunk_mode(
        self, interface: str, native_vlan: int, vlan_list: list[int], device: Device, password: str,
    ) -> dict:
        """Set *interface* to trunk mode with *native_vlan* (PVID) and
        *vlan_list*, atomically. *vlan_list* always fully replaces whatever
        the port had before (``undo ... all`` + set) — this is a mode
        change, not an add/remove relative to an existing trunk."""
        vlan_str = self._compress_vlans_huawei(sorted(set(vlan_list)))
        return self._aplicar_desde_template(
            "set_trunk_mode",
            {"interface": interface, "native_vlan": native_vlan, "allowed_vlans": vlan_str},
            device, password,
        )

    # ── VLAN list compression (Fase 2, A2 — movida desde validators/port_validator.py,
    # no es validación, es formato de CLI, específico de este vendor) ────────────

    def _compress_vlans_huawei(self, vlans: list[int]) -> str:
        """Format a VLAN list into the Huawei VRP trunk-allowed syntax.

        Example: [10, 11, 12, 20] → ``"10 to 12 20"``
        """
        parts = []
        for s, e in self._compress_to_ranges(vlans):
            parts.append(f"{s} to {e}" if s != e else str(s))
        return " ".join(parts)
