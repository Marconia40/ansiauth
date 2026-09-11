from __future__ import annotations

import ipaddress
import logging
import re
from typing import TYPE_CHECKING

from app.services.parsers._common import strip_known_preamble
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
_STORM_INDEX = 3

# RF-GLOBAL-01 -- línea de metadata al principio de "display
# current-configuration" que no es config real, confirmada en vivo contra
# f3r9s2 ("!Software Version ..."). Un "!"/"#" suelto de separador real del
# config no matchea esto, queda intacto.
_RUNNING_CONFIG_PREAMBLE = [
    r"^!Software Version.*$",
]


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

    # Same split as the port methods (see the note below): ``resolver_XXX``
    # decides op_key/variant/vars without touching the device, so
    # ``VLAN.resolver_paso()`` can batch N VLAN ops into 1 ``aplicar_lote()``
    # call. The public single-operation method delegates to it.

    def resolver_create_vlan(self, vlan_id: int, name: str) -> tuple[str, "str | None", dict]:
        return "create_vlan", None, {"vlan_id": vlan_id, "name": name}

    def create_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_create_vlan(vlan_id, name)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_delete_vlan(self, vlan_id: int) -> tuple[str, "str | None", dict]:
        return "delete_vlan", None, {"vlan_id": vlan_id}

    def delete_vlan(self, vlan_id: int, device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_delete_vlan(vlan_id)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_update_vlan(self, vlan_id: int, name: str) -> tuple[str, "str | None", dict]:
        return "update_vlan", None, {"vlan_id": vlan_id, "name": name}

    def update_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_update_vlan(vlan_id, name)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

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
        storm = stdouts[_STORM_INDEX] if len(stdouts) > _STORM_INDEX else ""
        try:
            ports = HuaweiPortParser.parse_ports(brief, description, port_vlan, storm)
        except Exception as exc:
            raise RuntimeError(f"Cannot determine port state on device '{device.name}': {exc}") from exc
        return ports

    # ── Port mutation operations ──────────────────────────────────────────────

    # Cada método de mutación se parte en "resolver_XXX" (decide op_key/
    # variant/vars -- SIN tocar el device) + el método público de siempre
    # (que sigue funcionando exactamente igual, solo delega). El resolver
    # es lo que ``Puerto.resolver_paso()`` llama para armar un paso de
    # ``aplicar_lote()`` sin ejecutar nada -- la sintaxis vendor-specific
    # (``expandir_nombre_interfaz``, conversión de threshold a int, etc.)
    # sigue viviendo acá, no se filtra al modelo de dominio.

    def resolver_update_port_description(self, interface: str, description: str) -> tuple[str, "str | None", dict]:
        variant = "clear" if self._is_description_empty(description) else "set"
        vars = {"interface": interface, "interface_full": expandir_nombre_interfaz(interface), "description": description}
        return "update_port_description", variant, vars

    def update_port_description(self, interface: str, description: str, device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_update_port_description(interface, description)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_port_admin_state(self, interface: str, enabled: bool) -> tuple[str, "str | None", dict]:
        variant = "enabled" if enabled else "disabled"
        vars = {"interface": interface, "interface_full": expandir_nombre_interfaz(interface)}
        return "set_port_admin_state", variant, vars

    def set_port_admin_state(self, interface: str, enabled: bool, device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_set_port_admin_state(interface, enabled)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_port_access_vlan(self, interface: str, vlan_id: int) -> tuple[str, "str | None", dict]:
        vars = {"interface": interface, "interface_full": expandir_nombre_interfaz(interface), "vlan_id": vlan_id}
        return "set_port_access_vlan", None, vars

    def set_port_access_vlan(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_set_port_access_vlan(interface, vlan_id)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_trunk_pvid_vlan(self, interface: str, vlan_id: int) -> tuple[str, "str | None", dict]:
        vars = {"interface": interface, "interface_full": expandir_nombre_interfaz(interface), "vlan_id": vlan_id}
        return "set_trunk_pvid_vlan", None, vars

    def set_trunk_pvid_vlan(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_set_trunk_pvid_vlan(interface, vlan_id)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_trunk_allowed_vlans(self, interface: str, vlan_list: list[int]) -> tuple[str, "str | None", dict]:
        vlan_str = self._compress_vlans_huawei(sorted(set(vlan_list)))
        vars = {
            "interface": interface, "interface_full": expandir_nombre_interfaz(interface),
            "allowed_vlans": vlan_str,
        }
        return "set_trunk_allowed_vlans", None, vars

    def set_trunk_allowed_vlans(self, interface: str, vlan_list: list[int], device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_set_trunk_allowed_vlans(interface, vlan_list)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_port_poe(self, interface: str, enabled: bool) -> tuple[str, "str | None", dict]:
        variant = "enabled" if enabled else "disabled"
        vars = {"interface": interface, "interface_full": expandir_nombre_interfaz(interface)}
        return "set_port_poe", variant, vars

    def set_port_poe(self, interface: str, enabled: bool, device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_set_port_poe(interface, enabled)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_storm_control(
        self, interface: str, enabled: bool, threshold: "float | None", action: str = "shutdown", trap: bool = True,
    ) -> tuple[str, "str | None", dict]:
        # "percent {threshold}" en VRP espera un entero -- confirmado
        # contra un device real: mandarlo como float de Python (ej. "1.0")
        # rompe el comando ("Unrecognized command" con basura de escape de
        # terminal en el medio). Cisco sí acepta decimales reales
        # (storm-control broadcast level 80.00), por eso esta conversión
        # queda acá y no en el modelo/schema compartido.
        variant = "enabled" if enabled else "disabled"
        # A diferencia de Cisco, VRP trata "action" como excluyente
        # (block|shutdown) y siempre la manda explícita -- nunca se omite,
        # ni siquiera para "filter"==block (a diferencia de Cisco, que sí
        # confía en su default implícito). 2 variables porque el YAML tiene
        # alternatives con y sin guión ("storm control"/"storm-control")
        # que coexisten según qué firmware acepta cuál -- mismo criterio ya
        # usado en las demás alternatives de este vendor.
        accion_vrp = "shutdown" if action == "shutdown" else "block"
        action_lines = [f"storm control action {accion_vrp}"] + (
            ["storm control enable trap"] if trap else []
        )
        action_lines_dash = [f"storm-control action {accion_vrp}"] + (
            ["storm-control enable trap"] if trap else []
        )
        vars = {
            "interface": interface, "interface_full": expandir_nombre_interfaz(interface),
            "threshold": int(threshold) if threshold is not None else None,
            "action_lines": action_lines, "action_lines_dash": action_lines_dash,
        }
        return "set_storm_control", variant, vars

    def set_storm_control(
        self, interface: str, enabled: bool, threshold: "float | None", action: str, trap: bool,
        device: Device, password: str,
    ) -> dict:
        op_key, variant, vars = self.resolver_set_storm_control(interface, enabled, threshold, action, trap)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_reset_port(self, interface: str) -> tuple[str, "str | None", dict]:
        vars = {"interface": interface, "interface_full": expandir_nombre_interfaz(interface)}
        return "reset_port", None, vars

    def reset_port(self, interface: str, device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_reset_port(interface)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    # ── Mode-change operations ────────────────────────────────────────────────

    def resolver_set_access_mode(self, interface: str, vlan_id: int) -> tuple[str, "str | None", dict]:
        vars = {"interface": interface, "interface_full": expandir_nombre_interfaz(interface), "vlan_id": vlan_id}
        return "set_access_mode", None, vars

    def set_access_mode(self, interface: str, vlan_id: int, device: Device, password: str) -> dict:
        """Set *interface* to access mode with *vlan_id*, atomically —
        ``port link-type access`` + ``port default vlan``, same single
        candidate-config session as every other mutation on this driver."""
        op_key, variant, vars = self.resolver_set_access_mode(interface, vlan_id)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_trunk_mode(
        self, interface: str, native_vlan: int, vlan_list: list[int],
    ) -> tuple[str, "str | None", dict]:
        vlan_str = self._compress_vlans_huawei(sorted(set(vlan_list)))
        vars = {
            "interface": interface, "interface_full": expandir_nombre_interfaz(interface),
            "native_vlan": native_vlan, "allowed_vlans": vlan_str,
        }
        return "set_trunk_mode", None, vars

    def set_trunk_mode(
        self, interface: str, native_vlan: int, vlan_list: list[int], device: Device, password: str,
    ) -> dict:
        """Set *interface* to trunk mode with *native_vlan* (PVID) and
        *vlan_list*, atomically. *vlan_list* always fully replaces whatever
        the port had before (``undo ... all`` + set) — this is a mode
        change, not an add/remove relative to an existing trunk."""
        op_key, variant, vars = self.resolver_set_trunk_mode(interface, native_vlan, vlan_list)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

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

    # Mismo criterio que huawei port driver (ver esa nota): resolver_XXX
    # (decide op_key/variant/vars, sin tocar el device) + método público.

    def resolver_set_svi_admin_state(self, vlan_id: int, enabled: bool) -> tuple[str, "str | None", dict]:
        variant = "enabled" if enabled else "disabled"
        return "set_svi_admin_state", variant, {"vlan_id": vlan_id}

    def set_svi_admin_state(self, vlan_id: int, enabled: bool, device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_set_svi_admin_state(vlan_id, enabled)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_svi_description(self, vlan_id: int, description: str) -> tuple[str, "str | None", dict]:
        variant = "clear" if self._is_description_empty(description) else "set"
        return "set_svi_description", variant, {"vlan_id": vlan_id, "description": description}

    def set_svi_description(self, vlan_id: int, description: str, device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_set_svi_description(vlan_id, description)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_svi_ipv4(self, vlan_id: int, ipv4_address: "str | None") -> tuple[str, "str | None", dict]:
        """*ipv4_address* llega en CIDR (``"10.10.10.11/24"``) o ``""``/
        ``None`` para limpiar -- VRP espera dirección + máscara punteada
        separadas, mismo criterio de conversión que Cisco."""
        variant = "clear" if not ipv4_address else "set"
        addr, mask = self._cidr_a_direccion_y_mascara(ipv4_address)
        return "set_svi_ipv4", variant, {"vlan_id": vlan_id, "ipv4_addr": addr, "ipv4_mask": mask}

    def set_svi_ipv4(self, vlan_id: int, ipv4_address: "str | None", device: Device, password: str) -> dict:
        op_key, variant, vars = self.resolver_set_svi_ipv4(vlan_id, ipv4_address)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_svi_ipv4_secondary(
        self, vlan_id: int, ipv4_address: "str | None", previous_ipv4_address: "str | None",
    ) -> tuple[str, "str | None", dict]:
        variant = "clear" if not ipv4_address else "set"
        addr, mask = self._cidr_a_direccion_y_mascara(ipv4_address if ipv4_address else previous_ipv4_address)
        return "set_svi_ipv4_secondary", variant, {"vlan_id": vlan_id, "ipv4_addr": addr, "ipv4_mask": mask}

    def set_svi_ipv4_secondary(
        self, vlan_id: int, ipv4_address: "str | None", previous_ipv4_address: "str | None",
        device: Device, password: str,
    ) -> dict:
        """VRP llama "sub" a la dirección secundaria (no confirmado contra
        device real). Igual que Cisco, limpiar necesita la dirección
        secundaria actual (*previous_ipv4_address*) para armar "undo ip
        address {addr} {mask} sub"."""
        op_key, variant, vars = self.resolver_set_svi_ipv4_secondary(vlan_id, ipv4_address, previous_ipv4_address)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_svi_ipv6(self, vlan_id: int, ipv6_address: "str | None") -> tuple[str, "str | None", dict]:
        variant = "clear" if not ipv6_address else "set"
        return "set_svi_ipv6", variant, {"vlan_id": vlan_id, "ipv6_address": ipv6_address or ""}

    def set_svi_ipv6(self, vlan_id: int, ipv6_address: "str | None", device: Device, password: str) -> dict:
        """Confirmado contra config real de un device de producción (no de
        lab): VRP usa CIDR de un tirón para ``ipv6 address``, igual que
        Cisco -- NO separa dirección/prefix-length como sí hace con IPv4
        (``ip address {addr} {mask}``). Ejemplo real:
        ``ipv6 address 2801:120:832::1/64`` sobre una interfaz con
        ``ipv6 enable`` ya puesto."""
        op_key, variant, vars = self.resolver_set_svi_ipv6(vlan_id, ipv6_address)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def set_svi_acl(
        self, vlan_id: int, direction: str, acl_name: "str | None", device: Device, password: str,
        *, current_acl_name: "str | None" = None,
    ) -> dict:
        """Confirmado EN VIVO contra f3r9s2: el orden de ``traffic-filter``
        es siempre ``{direction}bound acl ...`` -- lo que cambia entre
        numerada y con nombre es el keyword ``name`` (obligatorio antes de
        un nombre, ausente para un número):
        ``traffic-filter {direction}bound acl {numero}`` (ej.
        ``traffic-filter inbound acl 3002``) vs.
        ``traffic-filter {direction}bound acl name {nombre}`` (ej.
        ``traffic-filter inbound acl name test-acl``). Ver YAML
        (``set_svi_acl``) para el detalle de cómo se confirmó.

        Para limpiar (``acl_name`` vacío) VRP exige repetir la referencia
        EXACTA de la ACL que está atada -- confirmado en vivo que ``undo
        traffic-filter inbound`` solo, sin la ACL, es "Incomplete command".
        Por eso ``current_acl_name`` (lo que ``reconciliar()`` ya leyó del
        device) reemplaza a ``acl_name`` para armar el ``undo``, y
        numerada/con-nombre se decide sobre ESE valor, no sobre el nuevo
        (vacío). Si no hay nada atado (``current_acl_name`` también vacío)
        no hay nada que mandar -- el caller (``SVI._aplicar_acl``) ya lo
        trata como no-op, pero se cubre acá también por las dudas."""
        if not acl_name and not current_acl_name:
            return {"rc": 0, "stdout": "", "stderr": "", "success": True}
        op_key, variant, vars = self.resolver_set_svi_acl(
            vlan_id, direction, acl_name, current_acl_name=current_acl_name,
        )
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

    def resolver_set_svi_acl(
        self, vlan_id: int, direction: str, acl_name: "str | None", *, current_acl_name: "str | None" = None,
    ) -> tuple[str, "str | None", dict]:
        """Asume que ya se descartó el caso "nada atado y nada pedido"
        (ver ``set_svi_acl`` -- ese caso es no-op, el caller ya lo filtra
        antes de llegar acá vía ``SVI._resolver_acl``)."""
        if not acl_name:
            variant = "clear_numeric" if (current_acl_name or "").isdigit() else "clear_named"
            template_acl_name = current_acl_name or ""
        elif acl_name.isdigit():
            variant = "set_numeric"
            template_acl_name = acl_name
        else:
            variant = "set_named"
            template_acl_name = acl_name
        return "set_svi_acl", variant, {"vlan_id": vlan_id, "direction": direction, "acl_name": template_acl_name}

    def resolver_set_svi_dhcp_relay(
        self, vlan_id: int, servers: list[str],
    ) -> "tuple[str, str | None, dict]":
        return "set_svi_dhcp_relay", None, {"vlan_id": vlan_id, "servers": servers}

    def set_svi_dhcp_relay(
        self, vlan_id: int, servers: list[str], device: Device, password: str,
    ) -> dict:
        """Full-replace de la lista de relay servers -- ver YAML
        (``repeat``) para el "undo dhcp select relay" fijo + "dhcp select
        relay" (si hay al menos 1 server) + 1 línea por server, y la nota
        sobre la forma alternativa por "server group" ahí mismo."""
        op_key, variant, vars = self.resolver_set_svi_dhcp_relay(vlan_id, servers)
        return self._aplicar_desde_template(op_key, vars, device, password, variant=variant)

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

    def read_core_state(self, device: Device, password: str):
        """Fuse VLANs + ports + SVIs into 2 SSH sessions (down from 4):

        * Session A -- ``display vlan`` (1) + port cmds (4) + ``display
          ip interface brief`` (1) = 6 commands in a single ``_leer()``
          call. The brief output is what tells us which Vlanif interfaces
          exist -- VRP rejects the bulk ``display current-configuration
          interface Vlanif`` (no id), so discovery is unavoidable.
        * Session B -- ``display current-configuration interface
          Vlanif{id}`` for each discovered id + ``display
          current-configuration configuration dhcp`` at the end. Same
          shape as the second call in ``get_svis``.

        Kept at 2 sessions (rather than 1) because a single session
        would require dynamic-command generation inside the Ansible
        playbook, which is fragile and hard to test. 4 → 2 sessions is
        already the same 50% cut we get on Cisco.

        The individual methods stay untouched -- single-scope refreshes
        keep their current shape.
        """
        from app.services.parsers.svi_parser import HuaweiSVIParser
        from app.services.parsers.vlan_parser import parse_vrp_vlan_display

        cmds = self._cargar_comandos()
        vlan_cmds = list(cmds["list_vlans"]["primary"]["commands"])
        port_cmds = list(cmds["list_ports"]["primary"]["commands"])

        # Session A: fold VLAN + ports + Vlanif discovery into one call.
        session_a = vlan_cmds + port_cmds + ["display ip interface brief"]
        stdouts_a = self._leer(session_a, device, password)

        v_end = len(vlan_cmds)
        p_end = v_end + len(port_cmds)
        vlan_out = stdouts_a[:v_end]
        port_out = stdouts_a[v_end:p_end]
        brief = stdouts_a[p_end] if len(stdouts_a) > p_end else ""

        vlans = parse_vrp_vlan_display(vlan_out[0]) if vlan_out else []

        port_brief = port_out[_BRIEF_INDEX] if len(port_out) > _BRIEF_INDEX else ""
        description = port_out[_DESCRIPTION_INDEX] if len(port_out) > _DESCRIPTION_INDEX else ""
        port_vlan = port_out[_PORT_VLAN_INDEX] if len(port_out) > _PORT_VLAN_INDEX else ""
        storm = port_out[_STORM_INDEX] if len(port_out) > _STORM_INDEX else ""
        try:
            ports = HuaweiPortParser.parse_ports(port_brief, description, port_vlan, storm)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot determine port state on device '{device.name}': {exc}",
            ) from exc

        # Session B: per-Vlanif detail + dhcp, discovered from ``brief``.
        vlan_ids = sorted({int(m) for m in self._VLANIF_BRIEF_RE.findall(brief)})
        if not vlan_ids:
            dhcp_config = self._leer(
                ["display current-configuration configuration dhcp"], device, password,
            )[0]
            svis = HuaweiSVIParser.parse_svis(
                "", brief,
                dhcp_groups=HuaweiSVIParser.parse_dhcp_relay_groups(dhcp_config),
            )
        else:
            per_iface_commands = [
                f"display current-configuration interface Vlanif{vid}" for vid in vlan_ids
            ]
            per_iface_commands.append("display current-configuration configuration dhcp")
            outputs = self._leer(per_iface_commands, device, password)
            config = "\n".join(outputs[:-1])
            dhcp_groups = HuaweiSVIParser.parse_dhcp_relay_groups(outputs[-1])
            svis = HuaweiSVIParser.parse_svis(config, brief, dhcp_groups=dhcp_groups)

        return vlans, ports, svis

    def list_acl_names(self, device: Device, password: str) -> list[str]:
        commands = self._cargar_comandos()["list_acls"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        from app.services.parsers.svi_parser import parse_vrp_acl_names
        return parse_vrp_acl_names(stdouts[0] if stdouts else "")

    def list_acls(self, device: Device, password: str) -> list[dict]:
        """Como ``list_acl_names`` pero con las reglas de cada ACL (mismo
        comando ``display acl all``, no dispara una lectura aparte) --
        RF-GLOBAL-01/04, pedido tras ver la respuesta con ``acls`` como
        solo nombres. ``get_global_config()`` también usa este resultado
        para resolver ``snmp.trap_hosts`` en este vendor -- ver nota en
        ``set_snmp()``/``HuaweiGlobalConfigParser.parse()``."""
        commands = self._cargar_comandos()["list_acls"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        from app.services.parsers.svi_parser import parse_vrp_acls
        return parse_vrp_acls(stdouts[0] if stdouts else "")

    def _formatear_endpoint_acl(self, direction: str, endpoint: dict) -> "str | None":
        """*direction* ∈ {"source", "destination"}. ``None`` si el
        endpoint es ``any`` -- confirmado en vivo contra huawei01 que VRP
        OMITE la cláusula entera (ni "source any" ni nada equivalente)
        tanto al escribir (``rule permit icmp`` sin más ya es any/any)
        como al leer de vuelta, así que no escribirla es lo que hace que
        el no-op detection matchee después."""
        if endpoint.get("any"):
            return None
        if endpoint.get("host"):
            return f"{direction} {endpoint['host']} 0"
        network, wildcard = self._red_y_wildcard(endpoint["network"])
        return f"{direction} {network} {wildcard}"

    def formatear_regla_acl(self, rule: dict) -> str:
        """RF-GLOBAL-05. Arma el contenido de una regla (sin el prefijo
        ``rule`` que exige VRP al escribir) a partir de una regla
        vendor-agnóstica: ``{action} {protocol} [source {net} {wildcard}]
        [destination {net} {wildcard}] [destination-port eq|range ...]``.
        Se mantiene SIN ``rule`` a propósito -- esta misma string la usa
        ``GlobalConfig._regla_ya_presente()`` para no-op detection
        comparando por sufijo contra las reglas ya leídas (``"rule 5
        permit ..."``), y ``rule {N} `` es justo el prefijo variable que
        hay que poder recortar (Cisco es análogo con su número de
        secuencia). ``create_or_update_acl()``/``remove_acl_rules()``
        agregan el ``rule`` real recién al armar el comando de escritura
        -- confirmado en vivo que VRP lo exige ahí (``rule permit ...``,
        no ``permit ...`` suelto, "Unrecognized command" si falta). A
        diferencia de Cisco, el puerto SIEMPRE usa la keyword
        ``destination-port`` explícita (no un ``eq``/``range`` sueltos al
        final) -- alcance de esta vuelta cubre solo el caso "puerto de
        destino" (mismo que el ejemplo real del usuario), no
        ``source-port``."""
        partes = [rule["action"], rule["protocol"]]
        origen = self._formatear_endpoint_acl("source", rule["source"])
        if origen:
            partes.append(origen)
        destino = self._formatear_endpoint_acl("destination", rule["destination"])
        if destino:
            partes.append(destino)
        port = rule.get("port")
        if port:
            if port["operator"] == "range":
                partes.append(f"destination-port range {port['value']} {port['value2']}")
            else:
                partes.append(f"destination-port eq {port['value']}")
        return " ".join(partes)

    def create_or_update_acl(self, name: str, rule_lines: list[str], device: Device, password: str) -> dict:
        """RF-GLOBAL-05. Confirmado en vivo contra huawei01/f3r9s2: entrar
        a ``acl name {name} advance`` crea la ACL Advanced con nombre si
        no existía, y agrega las líneas si ya existía. Antepone ``rule ``
        a cada línea (``formatear_regla_acl()`` lo deja afuera a
        propósito, ver esa docstring) -- confirmado en vivo que VRP
        rechaza ``permit ...``/``deny ...`` sueltos ("Unrecognized
        command"), el verbo real de alta es ``rule permit/deny ...``.

        Bug de corrupción de terminal (mismo que bloqueaba ``trap_host``
        en ``set_snmp()``) -- RESUELTO vía ``screen-width 512`` al
        principio de la sesión (ver ``commands.yaml``): confirmado en
        vivo contra f3r9s2 que la regla que antes se corrompía
        (``destination-port eq 443``, con un nombre de ACL que empujaba
        el prompt+línea sobre el ancho de la terminal) ahora aplica
        limpia. ``screen-width`` no persiste en el running-config
        (confirmado en vivo) y no existe en todas las familias VRP --
        huawei01 (CE12800) lo rechaza, la 1ra alternativa del YAML cubre
        ese caso reintentando sin él."""
        rule_lines = [f"rule {linea}" for linea in rule_lines]
        return self._aplicar_desde_template(
            "create_or_update_acl", {"name": name, "rule_lines": rule_lines}, device, password,
        )

    def remove_acl_rules(self, name: str, rule_lines: list[str], device: Device, password: str) -> dict:
        """RF-GLOBAL-05 (delete de reglas puntuales). Confirmado en vivo
        contra huawei01/f3r9s2: ``undo rule {regla exacta}`` adentro del
        contexto de la ACL saca esa entrada puntual -- mismo criterio de
        anteponer ``rule`` que ``create_or_update_acl()``, ver esa
        docstring (acá el YAML antepone ``undo`` encima)."""
        rule_lines = [f"rule {linea}" for linea in rule_lines]
        return self._aplicar_desde_template(
            "remove_acl_rules", {"name": name, "rule_lines": rule_lines}, device, password,
        )

    def delete_acl(self, name: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-05 (delete de la ACL completa). Confirmado en vivo
        contra huawei01: ``undo acl name {name}`` desde system-view, sin
        necesidad de entrar al contexto de la ACL primero."""
        return self._aplicar_desde_template("delete_acl", {"name": name}, device, password)

    def get_global_config(self, device: Device, password: str) -> "GlobalConfig":
        """RF-GLOBAL-01/02/03/04 (SRS §3.4). "display snmp-agent sys-info"
        vive en un ``_leer()`` aparte (ver nota en
        ``commands.yaml: get_snmp_status``) -- mismo motivo que en Cisco:
        falla con rc != 0 cuando SNMP no está habilitado, y ``_leer()``
        aborta el batch entero ante el primer comando fallido. ACLs es 1
        lectura más, con su propio try/except. ARP/MAC NO viven acá --
        tienen su propio scope de sync (``ArpMacTables``/
        ``sync_arp_mac()``), a pedido del usuario."""
        commands = self._cargar_comandos()["get_global_config"]["primary"]["commands"]
        stdouts = self._leer(commands, device, password)
        version_output = stdouts[0] if len(stdouts) > 0 else ""
        hostname_output = stdouts[1] if len(stdouts) > 1 else ""
        route_output = stdouts[2] if len(stdouts) > 2 else ""
        running_config = stdouts[3] if len(stdouts) > 3 else ""

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
            acls = self.list_acls(device, password)
        except RuntimeError:
            logger.exception("get_global_config: list_acls failed on device=%s, continuing without ACLs", device.name)
            acls = None

        from app.services.parsers.global_config_parser import HuaweiGlobalConfigParser
        config = HuaweiGlobalConfigParser.parse(
            version_output=version_output, hostname_output=hostname_output,
            route_output=route_output, running_config_output=running_config,
            snmp_enabled=snmp_enabled, acls=acls,
        )
        config.device = device.name
        config.acls = acls
        config.running_config = strip_known_preamble(running_config, _RUNNING_CONFIG_PREAMBLE) or None
        return config

    def set_hostname(self, hostname: str, device: Device, password: str) -> dict:
        return self._aplicar_desde_template("set_hostname", {"hostname": hostname}, device, password)

    def set_snmp(self, cambios: dict, device: Device, password: str) -> dict:
        """RF-GLOBAL-07. A diferencia de Cisco, VRP sí tiene un comando de
        versión propio (``snmp-agent sys-info version``, aditivo -- ver
        comentario en ``commands.yaml``). ``community`` siempre se fija con
        el verbo "read" (ya no es parámetro, ver ``GlobalConfig.validar()``).
        ``trap_source`` confirmado en vivo contra f3r9s2 (``snmp-agent trap
        source {interface}``).

        ``trap_host`` -- el comando real (``snmp-agent target-host trap
        address udp-domain {ip} params securityname {community} v2c``) se
        había descartado en una vuelta anterior por corromperse en tránsito
        sobre esta sesión SSH (línea larga, se corta y re-envuelve con una
        secuencia de control ANSI en medio de una palabra). Confirmado en
        vivo esta vuelta contra f3r9s2, de punta a punta (alta + baja +
        ``display snmp-agent target-host`` para verificar), que con
        ``screen-width 512`` antepuesto (mismo fix ya usado en
        ``create_or_update_acl()``/``remove_acl_rules()`` para el mismo tipo
        de corrupción) el comando aplica bien -- la corrupción sigue
        apareciendo en el ECO visual (glitch de display del terminal), pero
        el parser de VRP interpreta el comando completo correctamente en
        los 2 sentidos (nunca tira "Unrecognized command"/"Invalid input"
        por la corrupción, y el estado final se verifica limpio). El
        workaround de ACL que se había explorado como alternativa (agregar
        la IP a la ACL atada al agente SNMP) ya NO hace falta -- VRP soporta
        trap hosts reales con community por-host, igual que Cisco.

        ``snmp.trap_hosts`` en la lectura (``get_global_config()``) sigue
        viniendo de cruzar la ACL del agente (``HuaweiGlobalConfigParser``)
        -- no de ``display snmp-agent target-host`` -- a propósito, sin
        tocar en esta vuelta: confirmado en vivo que la ACL reporta un host
        (200.16.16.13) que NO aparece como target-host real, o sea sirve un
        propósito más amplio que "solo trap hosts" en este device -- migrar
        la lectura cambiaría qué hosts se reportan hoy, un cambio de
        comportamiento que no se está pidiendo acá."""
        resultados = []
        if "version" in cambios:
            resultados.append(
                self._aplicar_desde_template("set_snmp_version", {"version": cambios["version"]}, device, password)
            )
        if "community" in cambios:
            resultados.append(
                self._aplicar_desde_template(
                    "set_snmp_community", {"verbo": "read", "community": cambios["community"]}, device, password,
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
                    {"host": cambios["trap_host"], "community": cambios["trap_host_community"]},
                    device, password,
                )
            )
        return self._combinar_resultados(resultados)

    def remove_snmp_trap_host(
        self, host: str, device: Device, password: str, *, community: "str | None" = None,
    ) -> dict:
        """RF-GLOBAL-07 (trap host, delete). A diferencia de Cisco, VRP
        exige la community EXACTA usada al agregar para poder armar el
        ``undo`` real -- confirmado en vivo contra f3r9s2 que un ``undo``
        con la community equivocada sale limpio pero rechazado ("Error: The
        specified target host does not exist."), no hay match por IP sola.
        Como la community queda cifrada al leerla de vuelta
        (``display snmp-agent target-host``), no hay forma de recuperarla
        del device -- si no viene acá, se levanta un error claro ANTES de
        tocar el device en vez de mandar un ``undo`` que sabemos que va a
        fallar."""
        if not community:
            raise ValueError(
                "HuaweiVendor.remove_snmp_trap_host: 'community' is required -- VRP needs the exact "
                "community used when the trap host was added to match and remove it, and it can't be "
                "read back from the device (stored encrypted)"
            )
        return self._aplicar_desde_template(
            "remove_snmp_trap_host", {"host": host, "community": community}, device, password,
        )

    def add_log_server(self, server: str, level: "str | None", device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (Log, endpoint propio). ``info-center loghost {ip}``
        confirmado en vivo. El nivel de severidad YA NO va en la misma
        línea del loghost (esa forma existe en huawei01 pero f3r9s2 la
        rechaza, "Too many parameters found") -- en cambio usa el
        mecanismo real de canales de VRP: ``info-center source default
        channel 2 log level {level}`` (canal 2 = "loghost", nombre fijo
        estándar en toda la familia VRP, confirmado con ``display
        channel`` contra f3r9s2) -- confirmado en vivo (idempotente, ya
        era el valor vigente ahí) contra f3r9s2 también, a diferencia de
        la forma inline que ese device rechazaba."""
        resultados = [self._aplicar_desde_template("add_log_host", {"server": server}, device, password)]
        if level:
            resultados.append(
                self._aplicar_desde_template("set_log_channel_level", {"level": level}, device, password)
            )
        return self._combinar_resultados(resultados)

    def remove_log_server(self, server: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (Log, delete). Confirmado en vivo contra huawei01."""
        return self._aplicar_desde_template("remove_log_host", {"server": server}, device, password)

    def get_arp_table(self, device: Device, password: str) -> list[dict]:
        """Ver docstring de ``CiscoVendor.get_arp_table()``, mismo
        criterio -- se lee completa (sin filtro) durante
        ``get_global_config()`` y se cachea, dejó de ser lectura en vivo
        por request. Parseado a filas estructuradas -- confirmado en vivo
        contra f3r9s2 (formato de 2 líneas por fila, ver
        ``arp_mac_parser.py``)."""
        from app.services.parsers.arp_mac_parser import parse_huawei_arp

        raw = self._leer(["display arp"], device, password)[0]
        return parse_huawei_arp(raw)

    def get_mac_table(self, device: Device, password: str) -> list[dict]:
        """Mismo criterio que ``get_arp_table()``."""
        from app.services.parsers.arp_mac_parser import parse_huawei_mac

        raw = self._leer(["display mac-address"], device, password)[0]
        return parse_huawei_mac(raw)

    def get_log_buffer(self, device: Device, password: str) -> str:
        """RF-GLOBAL fuera de alcance, pedido del usuario "de la misma
        forma que las tablas mac y arp". Confirmado en vivo contra
        f3r9s2: ``display logbuffer`` funciona limpio de punta a punta
        (~100 mil caracteres, 512 mensajes, sin cortes) -- a diferencia
        de Cisco (ver ``CiscoVendor.get_log_buffer()``), acá no hizo
        falta ningún exclude ni workaround."""
        raw = self._leer(["display logbuffer"], device, password)[0]
        return raw.strip()

    def set_route(self, destination: str, next_hop: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-06. Confirmado en vivo contra huawei01: ``ip
        route-static {network} {mask} {next_hop}``."""
        network, mask = self._red_y_mascara(destination)
        return self._aplicar_desde_template(
            "set_route", {"network": network, "mask": mask, "next_hop": next_hop}, device, password,
        )

    def remove_route(self, destination: str, next_hop: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-06 (delete). Confirmado en vivo contra huawei01 y f3r9s2."""
        network, mask = self._red_y_mascara(destination)
        return self._aplicar_desde_template(
            "remove_route", {"network": network, "mask": mask, "next_hop": next_hop}, device, password,
        )

    def add_ntp_server(self, server: str, prefer: bool, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (NTP, endpoint propio). ``prefer`` no tiene
        equivalente confirmado en VRP -- se ignora con un log (mismo
        criterio que ``version`` en ``set_snmp`` de Cisco)."""
        if prefer:
            logger.info(
                "HuaweiVendor.add_ntp_server: 'prefer' has no confirmed VRP equivalent, "
                "ignoring for device=%s",
                device.name,
            )
        return self._aplicar_desde_template("add_ntp", {"server": server}, device, password)

    def remove_ntp_server(self, server: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (NTP, delete). Confirmado en vivo contra huawei01."""
        return self._aplicar_desde_template("remove_ntp", {"server": server}, device, password)

    def add_dns_server(self, server: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (DNS, endpoint propio). Confirmado en vivo esta
        sesión: ``dns resolve`` + ``dns server {ip}``."""
        return self._aplicar_desde_template("add_dns", {"server": server}, device, password)

    def remove_dns_server(self, server: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (DNS, delete). Confirmado en vivo esta sesión."""
        return self._aplicar_desde_template("remove_dns", {"server": server}, device, password)

    def set_dns_domain(self, domain: str, device: Device, password: str) -> dict:
        """RF-GLOBAL-09 (DNS, domain-name). Confirmado en vivo contra
        f3r9s2 (idempotente, ya lo tiene configurado)."""
        return self._aplicar_desde_template("set_dns_domain", {"domain": domain}, device, password)

    @staticmethod
    def _cidr_a_direccion_y_mascara(cidr: "str | None") -> tuple[str, str]:
        """``"10.10.10.11/24"`` → ``("10.10.10.11", "255.255.255.0")`` --
        VRP espera máscara punteada, no CIDR, para ``ip address``. Con
        ``cidr`` vacío/``None`` (caso "limpiar") devuelve ``("", "")``."""
        if not cidr:
            return "", ""
        interfaz = ipaddress.ip_interface(cidr)
        return str(interfaz.ip), str(interfaz.netmask)
