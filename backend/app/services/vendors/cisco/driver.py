from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING

from app.services.parsers._common import strip_known_preamble
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
_STORM_INDEX = 3

# RF-GLOBAL-01 -- líneas de metadata al principio de "show running-config"
# que no son config real, confirmadas en vivo contra f3r9s1 ("Building
# configuration...", "Current configuration : N bytes", los 2 comentarios
# de "Last configuration change"/"NVRAM config last updated"). Un "!" suelto
# de separador real del config NO matchea ninguno de estos, queda intacto.
_RUNNING_CONFIG_PREAMBLE = [
    r"^Building configuration\.\.\.\s*$",
    r"^Current configuration\s*:.*bytes\s*$",
    r"^!\s*Last configuration change.*$",
    r"^!\s*NVRAM config last updated.*$",
]


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
        storm = stdouts[_STORM_INDEX] if len(stdouts) > _STORM_INDEX else ""
        try:
            ports = CiscoPortParser.parse_ports(status, description, switchport, storm)
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
        *, current_acl_name: "str | None" = None,
    ) -> dict:
        # current_acl_name unused -- "no ip access-group {direction}" clears
        # whatever is bound without needing to name it (only 1 ACL per
        # direction can ever be bound). See base.py's docstring.
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

    def read_core_state(self, device: Device, password: str):
        """Fuse VLANs + ports + SVIs into a single ``_leer()`` call -- 1
        SSH session instead of 3. Total commands = 1 (VLAN brief) + 4
        (port state) + 2 (SVI config + brief) = 7, all issued in the
        same ``ios_command`` task so the ``network_cli`` connection is
        opened once and reused for all seven.

        The individual methods (``list_vlans``/``list_ports``/
        ``get_svis``) remain untouched: single-scope refresh paths
        (``POST /devices/{name}/vlans/refresh`` etc.) continue calling
        them one at a time. This override only kicks in via
        ``DeviceSyncService.sync_core()`` on ``scope="all"``.
        """
        from app.services.parsers.svi_parser import CiscoSVIParser
        from app.services.parsers.vlan_parser import parse_vlan_brief

        cmds = self._cargar_comandos()
        vlan_cmds = list(cmds["list_vlans"]["primary"]["commands"])
        port_cmds = list(cmds["list_ports"]["primary"]["commands"])
        svi_cmds = list(cmds["get_svis"]["primary"]["commands"])

        combined = vlan_cmds + port_cmds + svi_cmds
        stdouts = self._leer(combined, device, password)

        # Slice back into the per-scope outputs, in the same order the
        # commands were appended above.
        v_end = len(vlan_cmds)
        p_end = v_end + len(port_cmds)
        vlan_out = stdouts[:v_end]
        port_out = stdouts[v_end:p_end]
        svi_out = stdouts[p_end:]

        vlans = parse_vlan_brief(vlan_out[0]) if vlan_out else []

        status = port_out[_STATUS_INDEX] if len(port_out) > _STATUS_INDEX else ""
        description = port_out[_DESCRIPTION_INDEX] if len(port_out) > _DESCRIPTION_INDEX else ""
        switchport = port_out[_SWITCHPORT_INDEX] if len(port_out) > _SWITCHPORT_INDEX else ""
        storm = port_out[_STORM_INDEX] if len(port_out) > _STORM_INDEX else ""
        try:
            ports = CiscoPortParser.parse_ports(status, description, switchport, storm)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot determine port state on device '{device.name}': {exc}",
            ) from exc

        running_config = svi_out[0] if len(svi_out) > 0 else ""
        brief = svi_out[1] if len(svi_out) > 1 else ""
        svis = CiscoSVIParser.parse_svis(running_config, brief)

        return vlans, ports, svis

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

    def list_acls(self, device: Device, password: str) -> list[dict]:
        """Como ``list_acl_names`` pero con las reglas de cada ACL (mismo
        comando ``show access-lists``, no dispara una lectura aparte) --
        RF-GLOBAL-01/04, pedido tras ver la respuesta con ``acls`` como
        solo nombres."""
        commands = self._cargar_comandos()["list_acls"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        from app.services.parsers.svi_parser import parse_ios_acls
        return parse_ios_acls(stdouts[0] if stdouts else "")

    def _formatear_endpoint_acl(self, endpoint: dict) -> str:
        if endpoint.get("any"):
            return "any"
        if endpoint.get("host"):
            return f"host {endpoint['host']}"
        network, wildcard = self._red_y_wildcard(endpoint["network"])
        return f"{network} {wildcard}"

    def formatear_regla_acl(self, rule: dict) -> str:
        """RF-GLOBAL-05. Arma la línea CLI real de IOS a partir de una
        regla vendor-agnóstica (``GlobalConfigAclRule``) -- confirmado en
        vivo contra cisco01: ``{action} {protocol} {source} {destination}
        [{port}]``, con ``source``/``destination`` resueltos a ``any`` /
        ``host {ip}`` / ``{network} {wildcard}`` (wildcard = inverso de la
        netmask, ver ``_red_y_wildcard()``). Sin secuencia -- IOS la
        auto-asigna al no incluirla."""
        partes = [
            rule["action"], rule["protocol"],
            self._formatear_endpoint_acl(rule["source"]),
            self._formatear_endpoint_acl(rule["destination"]),
        ]
        port = rule.get("port")
        if port:
            if port["operator"] == "range":
                partes.append(f"range {port['value']} {port['value2']}")
            else:
                partes.append(f"eq {port['value']}")
        return " ".join(partes)

    def create_or_update_acl(self, name: str, rule_lines: list[str], device: Device, password: str) -> dict:
        """RF-GLOBAL-05. Confirmado en vivo contra cisco01: entrar a ``ip
        access-list extended {name}`` crea la ACL si no existía, y agrega
        las líneas si ya existía -- mismo comando sirve para "crear" y
        "agregar reglas". Usa el mecanismo ``repeat`` de
        ``_ejecutar_paso()`` (ya existente, compartido con
        ``set_svi_dhcp_relay``) para mandar N reglas en 1 solo bloque de
        comando en vez de N round-trips."""
        return self._aplicar_desde_template(
            "create_or_update_acl", {"name": name, "rule_lines": rule_lines}, device, password,
        )

    def remove_acl_rules(self, name: str, rule_lines: list[str], device: Device, password: str) -> dict:
        """RF-GLOBAL-05 (delete de reglas puntuales). Confirmado en vivo
        contra cisco01: ``no {regla exacta}`` adentro del contexto de la
        ACL saca esa entrada puntual."""
        return self._aplicar_desde_template(
            "remove_acl_rules", {"name": name, "rule_lines": rule_lines}, device, password,
        )

    def delete_acl(self, name: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-05 (delete de la ACL completa). Confirmado en vivo
        contra cisco01: ``no ip access-list extended {name}``."""
        return self._aplicar_desde_template("delete_acl", {"name": name}, device, password)

    def get_global_config(self, device: Device, password: str) -> "GlobalConfig":
        """RF-GLOBAL-01/02/03/04 (SRS §3.4). "show snmp" vive en un
        ``_leer()`` aparte (ver nota en ``commands.yaml: get_snmp_status``)
        -- confirmado contra device real que falla con rc != 0 cuando SNMP
        no está habilitado, y que ``_leer()`` aborta el batch entero ante
        el primer comando fallido (perdería version/hostname/routes
        también si viviera en la misma tanda). ACLs es 1 lectura más, con
        su propio try/except (si falla, el resto sigue). ARP/MAC NO viven
        acá -- tienen su propio scope de sync (``ArpMacTables``/
        ``sync_arp_mac()``), a pedido del usuario (no hacen falta para
        ninguna escritura, y pueden traer muchísima info)."""
        commands = self._cargar_comandos()["get_global_config"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        version_output = stdouts[0] if len(stdouts) > 0 else ""
        hostname_output = stdouts[1] if len(stdouts) > 1 else ""
        community_output = stdouts[2] if len(stdouts) > 2 else ""
        route_output = stdouts[3] if len(stdouts) > 3 else ""
        running_config = stdouts[4] if len(stdouts) > 4 else ""

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
            acls = self.list_acls(device, password)
        except RuntimeError:
            logger.exception("get_global_config: list_acls failed on device=%s, continuing without ACLs", device.name)
            acls = None

        from app.services.parsers.global_config_parser import CiscoGlobalConfigParser
        config = CiscoGlobalConfigParser.parse(
            version_output=version_output, hostname_output=hostname_output,
            community_output=community_output, route_output=route_output,
            running_config_output=running_config, snmp_enabled=snmp_enabled,
        )
        config.device = device.name
        config.acls = acls
        config.running_config = strip_known_preamble(running_config, _RUNNING_CONFIG_PREAMBLE, separador="!") or None
        return config

    def set_hostname(self, hostname: str, device: Device, password: str) -> dict:
        return self._aplicar_desde_template("set_hostname", {"hostname": hostname}, device, password)

    def set_snmp(self, cambios: dict, device: Device, password: str) -> dict:
        """RF-GLOBAL-07. IOS clásico (SNMPv1/v2c basado en community) no
        tiene un comando separado para "versión" -- ``cambios["version"]``
        se ignora acá a propósito (ver comentario en ``commands.yaml``).
        ``community`` siempre se fija RO (ya no es parámetro). ``trap_source``
        y ``trap_host``(+``trap_version``+``trap_host_community``) confirmados
        en vivo contra f3r9s1 (aplicados idempotentemente contra los valores
        ya vigentes ahí, sin cambiar nada real)."""
        if "version" in cambios:
            logger.info(
                "CiscoVendor.set_snmp: 'version' has no distinct IOS community-based "
                "command, ignoring for device=%s",
                device.name,
            )
        resultados = []
        if "community" in cambios:
            resultados.append(
                self._aplicar_desde_template(
                    "set_snmp_community", {"community": cambios["community"]}, device, password,
                )
            )
        if "trap_source" in cambios:
            resultados.append(
                self._aplicar_desde_template(
                    "set_snmp_trap_source", {"interface": cambios["trap_source"]}, device, password,
                )
            )
        if "trap_host" in cambios:
            resultados.append(
                self._aplicar_desde_template(
                    "set_snmp_trap_host",
                    {
                        "host": cambios["trap_host"], "version": cambios["trap_version"],
                        "community": cambios["trap_host_community"],
                    },
                    device, password,
                )
            )
        return self._combinar_resultados(resultados)

    def add_log_server(self, server: str, level: "str | None", device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (Log, endpoint propio). Confirmado en vivo contra
        cisco01: ``logging host {ip}`` + (si viene ``level``) ``logging
        trap {level}`` -- en IOS el nivel sigue siendo un ajuste global
        (no por-host), pero viaja en el mismo request por conveniencia de
        API."""
        if level:
            return self._aplicar_desde_template(
                "set_log_host_and_level", {"log_server": server, "log_level": level}, device, password,
            )
        return self._aplicar_desde_template("set_log_host", {"log_server": server}, device, password)

    def remove_log_server(self, server: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (Log, delete). Confirmado en vivo contra cisco01."""
        return self._aplicar_desde_template("remove_log_host", {"log_server": server}, device, password)

    def get_arp_table(self, device: Device, password: str) -> list[dict]:
        """RF-GLOBAL fuera de alcance, pedido del usuario. Se lee completa
        (sin filtro) durante ``get_global_config()`` y se cachea -- dejó de
        ser lectura en vivo por request (única excepción a cache-first que
        tenía esta app) porque cada ``GET /arp`` pagaba el overhead
        completo de una sesión SSH/Ansible nueva; ``include`` ahora filtra
        en Python sobre el resultado ya cacheado (ver
        ``api/global_config.py``). Parseado a filas estructuradas,
        confirmado en vivo contra f3r9s1, ver ``arp_mac_parser.py``."""
        from app.services.parsers.arp_mac_parser import parse_cisco_arp

        raw = self._leer(["show arp"], device, password)[0]
        return parse_cisco_arp(raw)

    def get_mac_table(self, device: Device, password: str) -> list[dict]:
        """Mismo criterio que ``get_arp_table()``."""
        from app.services.parsers.arp_mac_parser import parse_cisco_mac

        raw = self._leer(["show mac address-table"], device, password)[0]
        return parse_cisco_mac(raw)

    def get_log_buffer(self, device: Device, password: str) -> str:
        """RF-GLOBAL fuera de alcance, pedido del usuario "de la misma
        forma que las tablas mac y arp". El ``exclude`` es necesario, NO
        opcional -- confirmado en vivo contra f3r9s1 que ``show logging``
        solo (o filtrado por cualquier otra cosa, ej. ``| include %``)
        corta la lectura a la mitad de una palabra y falla. Descartado
        timeout/tamaño como causa (probado con 3x el timeout normal,
        corte en el mismo punto exacto) -- el corte coincide siempre con
        una línea ``%PARSER-5-CFGLOG_LOGGEDCMD`` (IOS logea el texto
        completo de cada comando de configuración aplicado, incluye los
        propios de esta app, algunos largos) -- esa línea específica
        rompe la sesión SSH interactiva de esta lectura, mismo tipo de
        problema que la corrupción de terminal ya documentada en Huawei
        pero acá el contenido problemático lo genera el device, no
        nosotros. Confirmado en vivo que excluyéndola la lectura
        funciona limpia (~44 mil caracteres sin cortes) -- de paso, esas
        líneas son ruido de auditoría (ya lo tenemos en nuestro propio
        audit trail), no eventos operativos reales."""
        raw = self._leer(["show logging | exclude CFGLOG_LOGGEDCMD"], device, password)[0]
        return raw.strip()

    def set_route(self, destination: str, next_hop: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-06. Confirmado en vivo contra cisco01: ``ip route
        {network} {mask} {next_hop}``."""
        network, mask = self._red_y_mascara(destination)
        return self._aplicar_desde_template(
            "set_route", {"network": network, "mask": mask, "next_hop": next_hop}, device, password,
        )

    def remove_route(self, destination: str, next_hop: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-06 (delete). Confirmado en vivo contra cisco01."""
        network, mask = self._red_y_mascara(destination)
        return self._aplicar_desde_template(
            "remove_route", {"network": network, "mask": mask, "next_hop": next_hop}, device, password,
        )

    def add_ntp_server(self, server: str, prefer: bool, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (NTP, endpoint propio). Confirmado en vivo contra
        f3r9s1: ``ntp server {ip} [prefer]``."""
        op_key = "add_ntp_with_prefer" if prefer else "add_ntp"
        return self._aplicar_desde_template(op_key, {"server": server}, device, password)

    def remove_ntp_server(self, server: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (NTP, delete). Confirmado en vivo contra f3r9s1 --
        ``no ntp server {ip}`` saca la entrada aunque se haya agregado con
        ``prefer`` (no hace falta repetir el sufijo)."""
        return self._aplicar_desde_template("remove_ntp", {"server": server}, device, password)

    def add_dns_server(self, server: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (DNS, endpoint propio). Confirmado en vivo esta
        sesión: ``ip name-server {ip}``."""
        return self._aplicar_desde_template("add_dns", {"server": server}, device, password)

    def remove_dns_server(self, server: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (DNS, delete). Confirmado en vivo esta sesión."""
        return self._aplicar_desde_template("remove_dns", {"server": server}, device, password)

    def set_dns_domain(self, domain: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (DNS, domain-name). Confirmado en vivo contra
        f3r9s1 -- IOS normaliza ``ip domain-name`` a ``ip domain name`` en
        el running-config, pero acepta el alias al escribir."""
        return self._aplicar_desde_template("set_dns_domain", {"domain": domain}, device, password)

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
