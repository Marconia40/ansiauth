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
        block = f"system-view\nvlan {vlan_id}\ndescription {name}\nquit\ncommit\nquit"
        return self._aplicar({"command_block": block}, device, password, op_label=f"create VLAN {vlan_id}")

    def delete_vlan(self, vlan_id: int, device: Device, password: str) -> dict:
        block = f"system-view\nundo vlan {vlan_id}\ncommit\nquit"
        return self._aplicar({"command_block": block}, device, password, op_label=f"delete VLAN {vlan_id}")

    def update_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        block = f"system-view\nvlan {vlan_id}\ndescription {name}\nquit\ncommit\nquit"
        return self._aplicar({"command_block": block}, device, password, op_label=f"update VLAN {vlan_id}")

    def save_config(self, device: Device, password: str) -> dict:
        """Persist the running configuration via ``save force``.

        Explicit operation only — never invoked automatically by any
        mutation method in this class.
        """
        return self._aplicar({"command_block": "save force"}, device, password, op_label="save config")

    # ── VLAN query operations ─────────────────────────────────────────────────

    def list_vlans(self, device: Device, password: str) -> list[VLAN]:
        stdouts = self._leer(["display vlan"], device, password)
        from app.services.parsers.vlan_parser import parse_vrp_vlan_display
        return parse_vrp_vlan_display(stdouts[0])

    def get_vlans(self, device: Device, password: str) -> list[VLAN]:
        """Backward-compatible alias for ``list_vlans()``."""
        return self.list_vlans(device, password)

    # ── Port query operation ──────────────────────────────────────────────────

    def list_ports(self, device: Device, password: str) -> list[Puerto]:
        stdouts = self._leer(
            ["display interface brief", "display interface description", "display port vlan"],
            device, password,
        )
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
        is_empty = not bool(description and description.strip())
        line = "undo description" if is_empty else f"description {description}"
        block = f"system-view\ninterface {interface}\n{line}\ncommit\nquit\nquit"
        return self._aplicar({"command_block": block}, device, password, op_label="update port description")

    def set_port_admin_state(self, interface: str, enabled: bool, device: Device, password: str) -> dict:
        line = "undo shutdown" if enabled else "shutdown"
        block = f"system-view\ninterface {interface}\n{line}\ncommit\nquit\nquit"
        return self._aplicar({"command_block": block}, device, password, op_label=f"set admin state enabled={enabled}")

    def set_port_access_vlan(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        block = f"system-view\ninterface {interface}\nport default vlan {vlan_id}\ncommit\nquit\nquit"
        return self._aplicar({"command_block": block}, device, password, op_label="set access VLAN")

    def set_trunk_pvid_vlan(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        block = f"system-view\ninterface {interface}\nport trunk pvid vlan {vlan_id}\ncommit\nquit\nquit"
        return self._aplicar({"command_block": block}, device, password, op_label="set trunk native VLAN")

    def set_trunk_allowed_vlans(self, interface: str, vlan_list: list[int], device: Device, password: str) -> dict:
        vlan_str = self._compress_vlans_huawei(sorted(set(vlan_list)))
        block = (
            f"system-view\ninterface {interface}\n"
            f"undo port trunk allow-pass vlan all\nport trunk allow-pass vlan {vlan_str}\n"
            f"commit\nquit\nquit"
        )
        return self._aplicar({"command_block": block}, device, password, op_label="set trunk allowed VLANs")

    # ── Step 3.2 composite / semantic implementations ────────────────────────

    def set_access_mode(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        """Set *interface* to access mode with *vlan_id*, atomically.
        Thin wrapper over ``_configure_port()`` — same underlying VRP
        candidate-config session that already handled this combination
        before the generic ``configure_port()`` playbook was retired in
        favor of the shared ``run.yml``."""
        from app.models.port import Puerto
        puerto = Puerto(interface=interface, mode="access", access_vlan=vlan_id)
        return self._configure_port(puerto, device, password)

    def set_trunk_mode(
        self, interface: str, native_vlan: int, vlan_list: list[int], device: Device, password: str,
    ) -> dict:
        """Set *interface* to trunk mode with *native_vlan* (PVID) and
        *vlan_list*, atomically. Same criteria as ``set_access_mode()`` —
        thin wrapper over ``_configure_port()``, which already handles
        `access_vlan` as the trunk's PVID when `mode="trunk"`.

        ``allowed_vlan_operation="replace"`` is required here — a mode
        change always fully replaces the trunk's allowed-VLAN list (per
        this method's own contract), but ``Puerto.allowed_vlan_operation``
        defaults to ``"add"``. Without setting it explicitly,
        ``_configure_port()`` would union *vlan_list* with whatever was on
        the port before instead of replacing it — a real bug found while
        rewriting this method (pre-existing, not introduced by this
        rewrite: the old code never set it either); Cisco's implementation
        was unaffected since it always issues a direct CLI replace with no
        operation flag involved."""
        from app.models.port import Puerto
        puerto = Puerto(
            interface=interface, mode="trunk", access_vlan=native_vlan,
            allowed_vlans=list(vlan_list), allowed_vlan_operation="replace",
        )
        return self._configure_port(puerto, device, password)

    def _configure_port(self, config: Puerto, device: Device, password: str) -> dict:
        """Apply a composite set of port mutations on a Huawei VRP device,
        in a single candidate-config session committed atomically.

        Only the fields set on *config* are emitted — no mutation touches
        fields that weren't requested. Field application order (VRP
        constraint):
            1. ``port link-type`` (mode) — must precede VLAN commands.
            2. VLAN assignment (access or trunk, never both).
            3. ``description`` / ``undo description``.
            4. ``shutdown`` / ``undo shutdown`` (admin state).
            5. ``commit`` + ``quit``.

        Builds the full command block in Python instead of leaving the
        conditional assembly to Jinja inside the (now-retired)
        ``configure_port.yml`` — same logic, moved to the one place command
        construction lives for every other operation on this driver.
        """
        lines: list[str] = []

        if config.mode is not None:
            lines.append(f"port link-type {config.mode}")

        if config.access_vlan is not None:
            if config.mode == "trunk":
                lines.append(f"port trunk pvid vlan {config.access_vlan}")
            else:
                lines.append(f"port default vlan {config.access_vlan}")

        if config.allowed_vlans is not None:
            vlan_str = self._compress_vlans_huawei(sorted(set(config.allowed_vlans))) if config.allowed_vlans else ""
            operation = getattr(config, "allowed_vlan_operation", "add") or "add"
            if operation == "remove":
                lines.append(f"undo port trunk allow-pass vlan {vlan_str}")
            elif operation == "add":
                lines.append(f"port trunk allow-pass vlan {vlan_str}")
            else:
                lines.append("undo port trunk allow-pass vlan all")
                lines.append(f"port trunk allow-pass vlan {vlan_str}")

        if config.description is not None:
            is_empty = not bool(config.description and config.description.strip())
            lines.append("undo description" if is_empty else f"description {config.description}")

        # NOTE: previously read `config.admin_enabled`, an attribute Puerto
        # doesn't have (renamed to `admin_up` in an earlier session) — every
        # real call to this method raised AttributeError. Fixed here.
        if config.admin_up is not None:
            lines.append("undo shutdown" if config.admin_up else "shutdown")

        block = "\n".join(["system-view", f"interface {config.interface}", *lines, "commit", "quit", "quit"])
        return self._aplicar({"command_block": block}, device, password, op_label="configure_port")

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
