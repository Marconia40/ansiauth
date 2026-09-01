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
        line = "undo description" if self._is_description_empty(description) else f"description {description}"
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

    def set_port_poe(self, interface: str, enabled: bool, device: Device, password: str) -> dict:
        line = "poe enable" if enabled else "poe disable"
        block = f"system-view\ninterface {interface}\n{line}\ncommit\nquit\nquit"
        return self._aplicar({"command_block": block}, device, password, op_label=f"set PoE enabled={enabled}")

    def set_storm_control(
        self, interface: str, enabled: bool, threshold: "float | None", device: Device, password: str,
    ) -> dict:
        # Sintaxis pendiente de verificar contra el device real (varía por
        # familia de plataforma VRP) -- ver base.py:set_storm_control().
        line = f"storm-control broadcast {threshold}" if enabled else "undo storm-control broadcast"
        block = f"system-view\ninterface {interface}\n{line}\ncommit\nquit\nquit"
        return self._aplicar({"command_block": block}, device, password, op_label=f"set storm-control enabled={enabled}")

    def reset_port(self, interface: str, device: Device, password: str) -> dict:
        # "clear configuration interface" pide confirmación interactiva
        # ("Warning: ... Continue? [Y/N]") y se aplica de inmediato -- no
        # es un comando de candidate-config como el resto, no lleva
        # "commit" después. Confirmado contra el device real: con "commit"
        # en la línea siguiente, el device interpretaba eso como respuesta
        # inválida al prompt Y/N y quedaba reintentando hasta timeout.
        block = f"system-view\nclear configuration interface {interface}\ny\nquit"
        return self._aplicar({"command_block": block}, device, password, op_label="reset port to defaults")

    # ── Mode-change operations ────────────────────────────────────────────────

    def set_access_mode(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        """Set *interface* to access mode with *vlan_id*, atomically —
        ``port link-type access`` + ``port default vlan``, same single
        candidate-config session as every other mutation on this driver.
        No intermediate ``Puerto``/composite-builder step, same directness
        as ``CiscoVendor.set_access_mode()``."""
        block = (
            f"system-view\ninterface {interface}\n"
            f"port link-type access\nport default vlan {vlan_id}\n"
            f"commit\nquit\nquit"
        )
        return self._aplicar({"command_block": block}, device, password, op_label="set access mode")

    def set_trunk_mode(
        self, interface: str, native_vlan: int, vlan_list: list[int], device: Device, password: str,
    ) -> dict:
        """Set *interface* to trunk mode with *native_vlan* (PVID) and
        *vlan_list*, atomically. *vlan_list* always fully replaces whatever
        the port had before (``undo ... all`` + set) — this is a mode
        change, not an add/remove relative to an existing trunk."""
        vlan_str = self._compress_vlans_huawei(sorted(set(vlan_list)))
        block = (
            f"system-view\ninterface {interface}\n"
            f"port link-type trunk\nport trunk pvid vlan {native_vlan}\n"
            f"undo port trunk allow-pass vlan all\nport trunk allow-pass vlan {vlan_str}\n"
            f"commit\nquit\nquit"
        )
        return self._aplicar({"command_block": block}, device, password, op_label="set trunk mode")

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
