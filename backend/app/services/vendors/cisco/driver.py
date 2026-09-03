from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING

from app.services.parsers.cisco_port_parser import parse_ios_ports
from app.services.vendors.base import VendorDriver

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.svi import SVI
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

    The actual CLI command text lives in ``commands.yaml`` (next to this
    module), not here — real Cisco platforms vary in syntax (e.g. some
    need ``switchport trunk encapsulation dot1q`` before ``trunk`` mode,
    others reject that command outright), and editing a YAML to add a
    variant is a lot cheaper than editing this class and rebuilding.  Each
    method here only computes the substitution values (``vars``) and, for
    binary operations, which named variant to request —
    ``VendorDriver._aplicar_desde_template()`` does the rest (render,
    execute, retry known-error alternatives).

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
        """Persist the running configuration via ``write`` (exec-mode
        command, equivalent to ``copy running-config startup-config``).

        Explicit operation only — never invoked automatically by any
        mutation method in this class.
        """
        return self._aplicar_desde_template("save_config", {}, device, password)

    # ── VLAN query operations ─────────────────────────────────────────────────

    def list_vlans(self, device: Device, password: str) -> list[VLAN]:
        commands = self._cargar_comandos()["list_vlans"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        from app.services.parsers.vlan_parser import parse_vlan_brief
        vlans = parse_vlan_brief(stdouts[0])
        return vlans

    def get_vlans(self, device: Device, password: str) -> list[VLAN]:
        """Backward-compatible alias for ``list_vlans()``."""
        return self.list_vlans(device, password)

    # ── Port query operation ──────────────────────────────────────────────────

    def list_ports(self, device: Device, password: str) -> list[Puerto]:
        commands = self._cargar_comandos()["list_ports"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
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
        vlan_str = self._compress_vlans_cisco(sorted(set(vlan_list)))
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

    def set_access_mode(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar_desde_template(
            "set_access_mode", {"interface": interface, "vlan_id": vlan_id}, device, password,
        )

    def set_trunk_mode(
        self, interface: str, native_vlan: int, vlan_list: list[int], device: Device, password: str,
    ) -> dict:
        vlan_str = self._compress_vlans_cisco(sorted(set(vlan_list)))
        return self._aplicar_desde_template(
            "set_trunk_mode",
            {"interface": interface, "native_vlan": native_vlan, "allowed_vlans": vlan_str},
            device, password,
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

    # ── Virtual interface (SVI) operations, RF-INTERV-* ───────────────────────

    def create_svi(self, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar_desde_template("create_svi", {"vlan_id": vlan_id}, device, password)

    def delete_svi(self, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar_desde_template("delete_svi", {"vlan_id": vlan_id}, device, password)

    def set_svi_admin_state(self, vlan_id: int, enabled: bool, device: Device, password: str) -> dict:
        variant = "enabled" if enabled else "disabled"
        return self._aplicar_desde_template(
            "set_svi_admin_state", {"vlan_id": vlan_id}, device, password, variant=variant,
        )

    def set_svi_description(self, vlan_id: int, description: str, device: Device, password: str) -> dict:
        variant = "clear" if self._is_description_empty(description) else "set"
        return self._aplicar_desde_template(
            "set_svi_description", {"vlan_id": vlan_id, "description": description},
            device, password, variant=variant,
        )

    def set_svi_ipv4(self, vlan_id: int, ipv4_address: "str | None", device: Device, password: str) -> dict:
        """*ipv4_address* llega en CIDR (``"10.10.10.11/24"``) o ``""``/
        ``None`` para limpiar -- la conversión a máscara punteada (lo que
        IOS realmente espera) es cómputo real, se hace acá, no en el YAML."""
        variant = "clear" if not ipv4_address else "set"
        addr, mask = self._cidr_a_direccion_y_mascara(ipv4_address)
        return self._aplicar_desde_template(
            "set_svi_ipv4", {"vlan_id": vlan_id, "ipv4_addr": addr, "ipv4_mask": mask},
            device, password, variant=variant,
        )

    def set_svi_ipv4_secondary(
        self, vlan_id: int, ipv4_address: "str | None", previous_ipv4_address: "str | None",
        device: Device, password: str,
    ) -> dict:
        """Misma conversión CIDR->máscara que set_svi_ipv4() -- IOS
        pide "ip address {addr} {mask} secondary", y para limpiar hace
        falta repetir la dirección secundaria actual con "no" (a diferencia
        de la primaria, "no ip address" a secas borra TODO, no solo la
        secundaria) -- por eso el clear usa *previous_ipv4_address*, no
        *ipv4_address* (que viene vacío en ese caso)."""
        variant = "clear" if not ipv4_address else "set"
        addr, mask = self._cidr_a_direccion_y_mascara(ipv4_address if ipv4_address else previous_ipv4_address)
        return self._aplicar_desde_template(
            "set_svi_ipv4_secondary", {"vlan_id": vlan_id, "ipv4_addr": addr, "ipv4_mask": mask},
            device, password, variant=variant,
        )

    def set_svi_ipv6(self, vlan_id: int, ipv6_address: "str | None", device: Device, password: str) -> dict:
        """Cisco sí acepta CIDR directo para IPv6 -- no hace falta separar
        dirección/prefix como en IPv4."""
        variant = "clear" if not ipv6_address else "set"
        return self._aplicar_desde_template(
            "set_svi_ipv6", {"vlan_id": vlan_id, "ipv6_address": ipv6_address or ""},
            device, password, variant=variant,
        )

    def set_svi_acl(
        self, vlan_id: int, direction: str, acl_name: "str | None", device: Device, password: str,
    ) -> dict:
        variant = "clear" if not acl_name else "set"
        return self._aplicar_desde_template(
            "set_svi_acl", {"vlan_id": vlan_id, "direction": direction, "acl_name": acl_name or ""},
            device, password, variant=variant,
        )

    def set_svi_dhcp_relay(
        self, vlan_id: int, servers: list[str], device: Device, password: str,
    ) -> dict:
        """Full-replace de la lista de relay servers -- ver YAML
        (``repeat``) para el "no ip helper-address" fijo + 1 línea por
        server, y la nota sobre "match: none" ahí mismo."""
        return self._aplicar_desde_template(
            "set_svi_dhcp_relay", {"vlan_id": vlan_id, "servers": servers}, device, password,
        )

    def get_svis(self, device: Device, password: str) -> list[SVI]:
        commands = self._cargar_comandos()["get_svis"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        from app.services.parsers.svi_parser import parse_ios_svis
        running_config = stdouts[0] if len(stdouts) > 0 else ""
        brief = stdouts[1] if len(stdouts) > 1 else ""
        return parse_ios_svis(running_config, brief)

    def list_acl_names(self, device: Device, password: str) -> list[str]:
        """Confirmado contra el device real de lab con ACLs configuradas.
        Caso "0 ACLs" NO probado -- a diferencia de VRP (que sí imprime
        algo, "Total nonempty ACL number is 0", incluso vacío), no hay
        confirmación de si ``show access-lists`` en un device sin ninguna
        ACL devuelve stdout vacío (lo que ``_leer()`` trata como error de
        lectura, no como "0 ACLs") -- confirmar antes de asumir."""
        commands = self._cargar_comandos()["list_acls"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        from app.services.parsers.svi_parser import parse_ios_acl_names
        return parse_ios_acl_names(stdouts[0] if stdouts else "")

    @staticmethod
    def _cidr_a_direccion_y_mascara(cidr: "str | None") -> tuple[str, str]:
        """``"10.10.10.11/24"`` → ``("10.10.10.11", "255.255.255.0")`` --
        IOS espera máscara punteada, no CIDR, para ``ip address``. Con
        ``cidr`` vacío/``None`` (caso "limpiar") devuelve ``("", "")`` --
        el vendedor de la operación decide igual qué línea mandar según
        el variant, no según estos valores."""
        if not cidr:
            return "", ""
        interfaz = ipaddress.ip_interface(cidr)
        return str(interfaz.ip), str(interfaz.netmask)
