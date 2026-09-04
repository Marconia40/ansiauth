from __future__ import annotations

import ipaddress
import logging
import re
from typing import TYPE_CHECKING

from app.services.parsers.port_parser import HuaweiPortParser, expandir_nombre_interfaz
from app.services.vendors.base import VendorDriver

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.svi import SVI
    from app.models.port import Puerto
    from app.models.vlan import VLAN
    from app.models.global_config import GlobalConfig

logger = logging.getLogger(__name__)

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
            ports = HuaweiPortParser.parse_ports(brief, description, port_vlan)
        except Exception as exc:
            raise RuntimeError(f"Cannot determine port state on device '{device.name}': {exc}") from exc
        return ports

    # ── Port mutation operations ──────────────────────────────────────────────

    def update_port_description(self, interface: str, description: str, device: Device, password: str) -> dict:
        variant = "clear" if self._is_description_empty(description) else "set"
        return self._aplicar_desde_template(
            "update_port_description",
            {"interface": interface, "interface_full": expandir_nombre_interfaz(interface), "description": description},
            device, password, variant=variant,
        )

    def set_port_admin_state(self, interface: str, enabled: bool, device: Device, password: str) -> dict:
        variant = "enabled" if enabled else "disabled"
        return self._aplicar_desde_template(
            "set_port_admin_state",
            {"interface": interface, "interface_full": expandir_nombre_interfaz(interface)},
            device, password, variant=variant,
        )

    def set_port_access_vlan(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar_desde_template(
            "set_port_access_vlan",
            {"interface": interface, "interface_full": expandir_nombre_interfaz(interface), "vlan_id": vlan_id},
            device, password,
        )

    def set_trunk_pvid_vlan(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        return self._aplicar_desde_template(
            "set_trunk_pvid_vlan",
            {"interface": interface, "interface_full": expandir_nombre_interfaz(interface), "vlan_id": vlan_id},
            device, password,
        )

    def set_trunk_allowed_vlans(self, interface: str, vlan_list: list[int], device: Device, password: str) -> dict:
        vlan_str = self._compress_vlans_huawei(sorted(set(vlan_list)))
        return self._aplicar_desde_template(
            "set_trunk_allowed_vlans",
            {
                "interface": interface, "interface_full": expandir_nombre_interfaz(interface),
                "allowed_vlans": vlan_str,
            },
            device, password,
        )

    def set_port_poe(self, interface: str, enabled: bool, device: Device, password: str) -> dict:
        variant = "enabled" if enabled else "disabled"
        return self._aplicar_desde_template(
            "set_port_poe",
            {"interface": interface, "interface_full": expandir_nombre_interfaz(interface)},
            device, password, variant=variant,
        )

    def set_storm_control(
        self, interface: str, enabled: bool, threshold: "float | None", device: Device, password: str,
    ) -> dict:
        # "percent {threshold}" en VRP espera un entero -- confirmado
        # contra un device real: mandarlo como float de Python (ej. "1.0")
        # rompe el comando ("Unrecognized command" con basura de escape de
        # terminal en el medio). Cisco sí acepta decimales reales
        # (storm-control broadcast level 80.00), por eso esta conversión
        # queda acá y no en el modelo/schema compartido.
        variant = "enabled" if enabled else "disabled"
        vars = {
            "interface": interface, "interface_full": expandir_nombre_interfaz(interface),
            "threshold": int(threshold) if threshold is not None else None,
        }
        return self._aplicar_desde_template("set_storm_control", vars, device, password, variant=variant)

    def reset_port(self, interface: str, device: Device, password: str) -> dict:
        return self._aplicar_desde_template(
            "reset_port",
            {"interface": interface, "interface_full": expandir_nombre_interfaz(interface)},
            device, password,
        )

    # ── Mode-change operations ────────────────────────────────────────────────

    def set_access_mode(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        """Set *interface* to access mode with *vlan_id*, atomically —
        ``port link-type access`` + ``port default vlan``, same single
        candidate-config session as every other mutation on this driver."""
        return self._aplicar_desde_template(
            "set_access_mode",
            {"interface": interface, "interface_full": expandir_nombre_interfaz(interface), "vlan_id": vlan_id},
            device, password,
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
            {
                "interface": interface, "interface_full": expandir_nombre_interfaz(interface),
                "native_vlan": native_vlan, "allowed_vlans": vlan_str,
            },
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
        ``None`` para limpiar -- VRP espera dirección + máscara punteada
        separadas, mismo criterio de conversión que Cisco."""
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
        """VRP llama "sub" a la dirección secundaria (no confirmado contra
        device real). Igual que Cisco, limpiar necesita la dirección
        secundaria actual (*previous_ipv4_address*) para armar "undo ip
        address {addr} {mask} sub"."""
        variant = "clear" if not ipv4_address else "set"
        addr, mask = self._cidr_a_direccion_y_mascara(ipv4_address if ipv4_address else previous_ipv4_address)
        return self._aplicar_desde_template(
            "set_svi_ipv4_secondary", {"vlan_id": vlan_id, "ipv4_addr": addr, "ipv4_mask": mask},
            device, password, variant=variant,
        )

    def set_svi_ipv6(self, vlan_id: int, ipv6_address: "str | None", device: Device, password: str) -> dict:
        """Confirmado contra config real de un device de producción (no de
        lab): VRP usa CIDR de un tirón para ``ipv6 address``, igual que
        Cisco -- NO separa dirección/prefix-length como sí hace con IPv4
        (``ip address {addr} {mask}``). Ejemplo real:
        ``ipv6 address 2801:120:832::1/64`` sobre una interfaz con
        ``ipv6 enable`` ya puesto."""
        variant = "clear" if not ipv6_address else "set"
        return self._aplicar_desde_template(
            "set_svi_ipv6", {"vlan_id": vlan_id, "ipv6_address": ipv6_address or ""},
            device, password, variant=variant,
        )

    def set_svi_acl(
        self, vlan_id: int, direction: str, acl_name: "str | None", device: Device, password: str,
    ) -> dict:
        """Confirmado contra config real: el orden de ``traffic-filter``
        cambia según si la ACL es numerada o con nombre --
        ``traffic-filter {direction}bound acl {numero}`` para numeradas
        (ej. ``traffic-filter inbound acl 3002``), pero
        ``traffic-filter acl {nombre} {direction}bound`` para ACLs con
        nombre (ej. ``traffic-filter acl servers-admin-dc2-vlan830
        inbound``) -- el nombre va pegado a "acl" y la dirección al final,
        no como en el caso numerado."""
        if not acl_name:
            variant = "clear"
        elif acl_name.isdigit():
            variant = "set_numeric"
        else:
            variant = "set_named"
        return self._aplicar_desde_template(
            "set_svi_acl", {"vlan_id": vlan_id, "direction": direction, "acl_name": acl_name or ""},
            device, password, variant=variant,
        )

    def set_svi_dhcp_relay(
        self, vlan_id: int, servers: list[str], device: Device, password: str,
    ) -> dict:
        """Full-replace de la lista de relay servers -- ver YAML
        (``repeat``) para el "undo dhcp select relay" fijo + "dhcp select
        relay" (si hay al menos 1 server) + 1 línea por server, y la nota
        sobre la forma alternativa por "server group" ahí mismo."""
        return self._aplicar_desde_template(
            "set_svi_dhcp_relay", {"vlan_id": vlan_id, "servers": servers}, device, password,
        )

    _VLANIF_BRIEF_RE = re.compile(r"^Vlanif(\d+)\b", re.MULTILINE)

    def get_svis(self, device: Device, password: str) -> list[SVI]:
        """Confirmado contra el device real de lab: VRP rechaza el comando
        bulk "display current-configuration interface Vlanif" sin número
        ("Error: Wrong parameter found at '^' position") -- a diferencia
        de Cisco, acá hace falta 2 pasadas: primero ``display ip interface
        brief`` para descubrir qué Vlanif existen, después un ``display
        current-configuration interface Vlanif{id}`` por cada una
        encontrada (confirmado que esta forma con número sí funciona).
        Bypassa el YAML para este operación -- el número de comandos es
        dinámico según lo que reporte el device, mismo criterio que
        ``set_svi_dhcp_relay()``.

        Agrega ``display current-configuration configuration dhcp`` a la
        misma tanda (confirmado contra el device real de lab) -- resuelve
        la forma "server group" del DHCP relay, la única que existe en
        este CE12800 (ver nota en ``svi_parser._VRP_HELPER``, bug real
        encontrado probando el incremental-add en vivo: sin esto,
        reconciliar() nunca veía los servers ya configurados por esa
        forma)."""
        brief = self._leer(["display ip interface brief"], device, password)[0]
        vlan_ids = sorted({int(m) for m in self._VLANIF_BRIEF_RE.findall(brief)})
        from app.services.parsers.svi_parser import HuaweiSVIParser
        if not vlan_ids:
            dhcp_config = self._leer(["display current-configuration configuration dhcp"], device, password)[0]
            return HuaweiSVIParser.parse_svis("", brief, dhcp_groups=HuaweiSVIParser.parse_dhcp_relay_groups(dhcp_config))
        per_iface_commands = [f"display current-configuration interface Vlanif{vid}" for vid in vlan_ids]
        per_iface_commands.append("display current-configuration configuration dhcp")
        outputs = self._leer(per_iface_commands, device, password)
        config = "\n".join(outputs[:-1])
        dhcp_groups = HuaweiSVIParser.parse_dhcp_relay_groups(outputs[-1])
        return HuaweiSVIParser.parse_svis(config, brief, dhcp_groups=dhcp_groups)

    def list_acl_names(self, device: Device, password: str) -> list[str]:
        commands = self._cargar_comandos()["list_acls"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        from app.services.parsers.svi_parser import parse_vrp_acl_names
        return parse_vrp_acl_names(stdouts[0] if stdouts else "")

    def get_global_config(self, device: Device, password: str) -> "GlobalConfig":
        """RF-GLOBAL-01/02/03/04 (SRS §3.4). "display snmp-agent sys-info"
        vive en un ``_leer()`` aparte (ver nota en
        ``commands.yaml: get_snmp_status``) -- mismo motivo que en Cisco:
        falla con rc != 0 cuando SNMP no está habilitado, y ``_leer()``
        aborta el batch entero ante el primer comando fallido."""
        commands = self._cargar_comandos()["get_global_config"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        version_output = stdouts[0] if len(stdouts) > 0 else ""
        hostname_output = stdouts[1] if len(stdouts) > 1 else ""
        route_output = stdouts[2] if len(stdouts) > 2 else ""

        snmp_commands = self._cargar_comandos()["get_snmp_status"]["primary"]["commands"]
        try:
            self._leer(snmp_commands, device, password)
            snmp_enabled = True
        except RuntimeError as exc:
            if "snmp agent is not enabled" in str(exc).lower():
                snmp_enabled = False
            else:
                raise

        try:
            acls = self.list_acl_names(device, password)
        except RuntimeError:
            logger.exception("get_global_config: list_acl_names failed on device=%s, continuing without ACLs", device.name)
            acls = None

        from app.services.parsers.global_config_parser import HuaweiGlobalConfigParser
        config = HuaweiGlobalConfigParser.parse(
            version_output=version_output, hostname_output=hostname_output,
            route_output=route_output, snmp_enabled=snmp_enabled,
        )
        config.device = device.name
        config.acls = acls
        return config

    def set_hostname(self, hostname: str, device: Device, password: str) -> dict:
        return self._aplicar_desde_template("set_hostname", {"hostname": hostname}, device, password)

    def set_snmp(self, cambios: dict, device: Device, password: str) -> dict:
        """RF-GLOBAL-07. A diferencia de Cisco, VRP sí tiene un comando de
        versión propio (``snmp-agent sys-info version``, aditivo -- ver
        comentario en ``commands.yaml``) y uno de community separado
        (``snmp-agent community {read|write} ...``, el verbo YA es el
        permiso). ``cambios`` puede traer 1 o los 2 -- se ejecutan como 2
        template calls independientes (cada uno con su propio
        alternativa "sin commit" para f3r9s2)."""
        resultados = []
        if "version" in cambios:
            resultados.append(
                self._aplicar_desde_template("set_snmp_version", {"version": cambios["version"]}, device, password)
            )
        if "community" in cambios:
            verbo = "write" if cambios.get("permission") == "RW" else "read"
            resultados.append(
                self._aplicar_desde_template(
                    "set_snmp_community", {"verbo": verbo, "community": cambios["community"]}, device, password,
                )
            )
        return self._combinar_resultados(resultados)

    def set_log_servers(self, cambios: dict, device: Device, password: str) -> dict:
        """RF-GLOBAL-09. Confirmado en vivo contra huawei01: ``ntp
        unicast-server``, ``dns resolve``+``dns server``, ``info-center
        loghost {ip} [level {level}]``. VRP ata el nivel de severidad a la
        línea del loghost (no es un ajuste global independiente como en
        IOS) -- por eso ``GlobalConfig.validar()`` exige que ``log_level``
        venga siempre junto con ``log_server``."""
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
        """RF-GLOBAL-06. Confirmado en vivo contra huawei01: ``ip
        route-static {network} {mask} {next_hop}``."""
        network, mask = self._red_y_mascara(destination)
        return self._aplicar_desde_template(
            "set_route", {"network": network, "mask": mask, "next_hop": next_hop}, device, password,
        )

    @staticmethod
    def _cidr_a_direccion_y_mascara(cidr: "str | None") -> tuple[str, str]:
        """``"10.10.10.11/24"`` → ``("10.10.10.11", "255.255.255.0")`` --
        VRP espera máscara punteada, no CIDR, para ``ip address``. Con
        ``cidr`` vacío/``None`` (caso "limpiar") devuelve ``("", "")``."""
        if not cidr:
            return "", ""
        interfaz = ipaddress.ip_interface(cidr)
        return str(interfaz.ip), str(interfaz.netmask)
