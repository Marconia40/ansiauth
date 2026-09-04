from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING

from app.services.parsers.port_parser import CiscoPortParser
from app.services.vendors.base import VendorDriver

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.svi import SVI
    from app.models.port import Puerto
    from app.models.vlan import VLAN
    from app.models.global_config import GlobalConfig

logger = logging.getLogger(__name__)

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
            ports = CiscoPortParser.parse_ports(status, description, switchport)
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
        from app.services.parsers.svi_parser import CiscoSVIParser
        running_config = stdouts[0] if len(stdouts) > 0 else ""
        brief = stdouts[1] if len(stdouts) > 1 else ""
        return CiscoSVIParser.parse_svis(running_config, brief)

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

    def get_global_config(self, device: Device, password: str) -> "GlobalConfig":
        """RF-GLOBAL-01/02/03/04 (SRS §3.4). "show snmp" vive en un
        ``_leer()`` aparte (ver nota en ``commands.yaml: get_snmp_status``)
        -- confirmado contra device real que falla con rc != 0 cuando SNMP
        no está habilitado, y que ``_leer()`` aborta el batch entero ante
        el primer comando fallido (perdería version/hostname/routes
        también si viviera en la misma tanda)."""
        commands = self._cargar_comandos()["get_global_config"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        version_output = stdouts[0] if len(stdouts) > 0 else ""
        hostname_output = stdouts[1] if len(stdouts) > 1 else ""
        community_output = stdouts[2] if len(stdouts) > 2 else ""
        route_output = stdouts[3] if len(stdouts) > 3 else ""

        snmp_commands = self._cargar_comandos()["get_snmp_status"]["primary"]["commands"]
        try:
            self._leer(snmp_commands, device, password)
            snmp_enabled = True
        except RuntimeError as exc:
            if "snmp agent not enabled" in str(exc).lower():
                snmp_enabled = False
            else:
                raise

        try:
            acls = self.list_acl_names(device, password)
        except RuntimeError:
            logger.exception("get_global_config: list_acl_names failed on device=%s, continuing without ACLs", device.name)
            acls = None

        from app.services.parsers.global_config_parser import CiscoGlobalConfigParser
        config = CiscoGlobalConfigParser.parse(
            version_output=version_output, hostname_output=hostname_output,
            community_output=community_output, route_output=route_output,
            snmp_enabled=snmp_enabled,
        )
        config.device = device.name
        config.acls = acls
        return config

    def set_hostname(self, hostname: str, device: Device, password: str) -> dict:
        return self._aplicar_desde_template("set_hostname", {"hostname": hostname}, device, password)

    def set_snmp(self, cambios: dict, device: Device, password: str) -> dict:
        """RF-GLOBAL-07. IOS clásico (SNMPv1/v2c basado en community) no
        tiene un comando separado para "versión" -- ``cambios["version"]``
        se ignora acá a propósito (ver comentario en ``commands.yaml``);
        solo ``community``+``permission`` (siempre juntos, ver
        ``GlobalConfig.validar()``) disparan un comando real."""
        if "version" in cambios:
            logger.info(
                "CiscoVendor.set_snmp: 'version' has no distinct IOS community-based "
                "command, ignoring for device=%s (only community/permission apply)",
                device.name,
            )
        if "community" not in cambios:
            return {"rc": 0, "stdout": "", "stderr": "", "success": True, "changed": False}
        return self._aplicar_desde_template(
            "set_snmp_community",
            {"community": cambios["community"], "permission": cambios["permission"]},
            device, password,
        )

    def set_log_servers(self, cambios: dict, device: Device, password: str) -> dict:
        """RF-GLOBAL-09. Confirmado en vivo contra cisco01: ``ntp server``,
        ``ip name-server``, ``logging host``/``logging trap {level}``."""
        resultados = []
        if "ntp_server" in cambios:
            resultados.append(
                self._aplicar_desde_template("set_ntp", {"ntp_server": cambios["ntp_server"]}, device, password)
            )
        if "dns_server" in cambios:
            resultados.append(
                self._aplicar_desde_template("set_dns", {"dns_server": cambios["dns_server"]}, device, password)
            )
        if "log_server" in cambios:
            if "log_level" in cambios:
                resultados.append(
                    self._aplicar_desde_template(
                        "set_log_host_and_level",
                        {"log_server": cambios["log_server"], "log_level": cambios["log_level"]},
                        device, password,
                    )
                )
            else:
                resultados.append(
                    self._aplicar_desde_template("set_log_host", {"log_server": cambios["log_server"]}, device, password)
                )
        return self._combinar_resultados(resultados)

    def set_route(self, destination: str, next_hop: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-06. Confirmado en vivo contra cisco01: ``ip route
        {network} {mask} {next_hop}``."""
        network, mask = self._red_y_mascara(destination)
        return self._aplicar_desde_template(
            "set_route", {"network": network, "mask": mask, "next_hop": next_hop}, device, password,
        )

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
