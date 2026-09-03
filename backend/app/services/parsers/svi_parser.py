"""Parsers para el output real de ``get_svis`` -- RF-INTERV-07.

Igual que el resto de los parsers de este paquete, están escritos contra el
formato de output *documentado* de cada plataforma, no contra una captura
verificada de un device real corriendo estos comandos exactos (a diferencia
de ``vlan_parser.py``/``cisco_port_parser.py``, que sí lo están). Marcar
como pendiente de confirmar antes de confiar ciegamente en el parseo --
mismo criterio que el resto de esta sesión con sintaxis no verificada.
"""
from __future__ import annotations

import ipaddress
import re

from app.models.svi import SVI

_ANSI_ESCAPE = re.compile(r"\x1B\[[0-9;]*m")


def _direccion_y_mascara_a_cidr(addr: str, mascara: str) -> str:
    """``("192.168.1.1", "255.255.255.0")`` -> ``"192.168.1.1/24"`` --
    confirmado contra running-config real (Cisco IOS: ``ip address {addr}
    {dotted-mask}``, VRP tiene el mismo formato) que ``ip_interface()``
    acepta una máscara punteada en la parte "/" y la normaliza a
    prefix-length sola. El resto del sistema (schemas, comparación de
    no-op en ``SVI._aplicar_ipv4``) espera CIDR, no
    "dirección/máscara-punteada" -- unirlas con un simple f-string (bug
    real encontrado corriendo esto contra el device de lab) rompía ese
    contrato silenciosamente."""
    try:
        return str(ipaddress.ip_interface(f"{addr}/{mascara}"))
    except ValueError:
        return f"{addr}/{mascara}"


def _normalizar_ipv6(cidr: str) -> str:
    """VRP devuelve las IPv6 en MAYÚSCULAS (``2001:DB8::1/64``) aunque se
    hayan configurado en minúsculas -- confirmado contra el device real de
    lab. Sin normalizar, la comparación de no-op en
    ``SVI._aplicar_ipv6`` (``actual.ipv6_address ==
    self.ipv6_address``) nunca matchea, y cada PATCH reenvía el comando al
    device aunque el valor ya sea el mismo. ``ipaddress`` normaliza a
    minúsculas, igual que el resto de este sistema."""
    try:
        return str(ipaddress.ip_interface(cidr))
    except ValueError:
        return cidr


# ── Cisco IOS ────────────────────────────────────────────────────────────────

# "interface Vlan10" -- arranca un bloque nuevo en el running-config filtrado.
_IOS_IFACE_HEADER = re.compile(r"^interface\s+Vlan(\d+)\s*$", re.IGNORECASE)
_IOS_DESCRIPTION = re.compile(r"^\s*description\s+(.+?)\s*$")
_IOS_SHUTDOWN = re.compile(r"^\s*shutdown\s*$")
# La secundaria debe chequearse antes que la primaria -- misma línea base
# ("ip address X Y"), solo se distinguen por el sufijo "secondary".
_IOS_IPV4_SECONDARY = re.compile(r"^\s*ip address\s+(\S+)\s+(\S+)\s+secondary\s*$", re.IGNORECASE)
_IOS_IPV4 = re.compile(r"^\s*ip address\s+(\S+)\s+(\S+)\s*$")
_IOS_IPV6 = re.compile(r"^\s*ipv6 address\s+(\S+)\s*$")
_IOS_ACL = re.compile(r"^\s*ip access-group\s+(\S+)\s+(in|out)\s*$", re.IGNORECASE)
_IOS_HELPER = re.compile(r"^\s*ip helper-address\s+(\S+)\s*$")

# "Vlan10 ... up" / "Vlan10 ... administratively down" -- de
# 'show ip interface brief'. El estado admin real está en la ÚLTIMA
# columna de "Status" (protocol es la de más a la derecha del todo).
_IOS_BRIEF_LINE = re.compile(
    r"^Vlan(\d+)\s+\S+\s+\S+\s+(?:manual|dhcp|other|unset)\s+(.+?)\s+(up|down)\s*$",
    re.IGNORECASE,
)


def parse_ios_svis(running_config_output: str, brief_output: str) -> list[SVI]:
    """Parse ``show running-config | section ^interface Vlan`` +
    ``show ip interface brief | include Vlan`` en ``SVI``.

    El primero trae la config deseada (ip/ipv6, description, ACLs,
    helper-address); el segundo el estado administrativo real (running-config
    no siempre expone ``shutdown``/``no shutdown`` de forma consistente
    entre versiones de IOS)."""
    estado_por_vlan: dict[int, tuple[bool, bool]] = {}
    for raw in brief_output.splitlines():
        line = _ANSI_ESCAPE.sub("", raw).rstrip()
        m = _IOS_BRIEF_LINE.match(line)
        if not m:
            continue
        vlan_id, status_text, protocol = int(m.group(1)), m.group(2), m.group(3)
        admin_up = "administratively down" not in status_text.lower()
        operational_up = protocol.lower() == "up"
        estado_por_vlan[vlan_id] = (admin_up, operational_up)

    interfaces: list[SVI] = []
    actual: SVI | None = None
    helpers: list[str] = []

    def _cerrar_actual() -> None:
        if actual is not None:
            actual.dhcp_relay_servers = helpers[:] if helpers else None
            interfaces.append(actual)

    for raw in running_config_output.splitlines():
        line = _ANSI_ESCAPE.sub("", raw).rstrip()
        header = _IOS_IFACE_HEADER.match(line)
        if header:
            _cerrar_actual()
            vlan_id = int(header.group(1))
            admin_up, operational_up = estado_por_vlan.get(vlan_id, (None, None))
            actual = SVI(vlan_id=vlan_id, admin_up=admin_up, operational_up=operational_up)
            helpers = []
            continue
        if actual is None:
            continue
        if _IOS_SHUTDOWN.match(line):
            actual.admin_up = False
            continue
        m = _IOS_DESCRIPTION.match(line)
        if m:
            actual.description = m.group(1)
            continue
        m = _IOS_IPV4_SECONDARY.match(line)
        if m:
            actual.ipv4_address_secondary = _direccion_y_mascara_a_cidr(m.group(1), m.group(2))
            continue
        m = _IOS_IPV4.match(line)
        if m:
            actual.ipv4_address = _direccion_y_mascara_a_cidr(m.group(1), m.group(2))
            continue
        m = _IOS_IPV6.match(line)
        if m:
            actual.ipv6_address = m.group(1)
            continue
        m = _IOS_ACL.match(line)
        if m:
            if m.group(2).lower() == "in":
                actual.acl_in = m.group(1)
            else:
                actual.acl_out = m.group(1)
            continue
        m = _IOS_HELPER.match(line)
        if m:
            helpers.append(m.group(1))
            continue

    _cerrar_actual()
    return interfaces


# ── Huawei VRP ───────────────────────────────────────────────────────────────
# Header/description/shutdown/ip address (IPv4) y el formato de brief
# confirmados contra el device real de lab (ver driver.py -- 2 pasadas,
# "display current-configuration interface Vlanif{id}" con número +
# "display ip interface brief"). ipv6 address / traffic-filter (ambas
# formas, numerada y con nombre) confirmados aparte contra config real de
# un device de producción. dhcp select relay/server-ip -- ver
# huawei/commands.yaml.

_VRP_IFACE_HEADER = re.compile(r"^interface\s+Vlanif(\d+)\s*$", re.IGNORECASE)
_VRP_DESCRIPTION = re.compile(r"^\s*description\s+(.+?)\s*$")
_VRP_SHUTDOWN = re.compile(r"^\s*shutdown\s*$")
# VRP llama "sub" a la dirección secundaria (a diferencia de Cisco
# "secondary") -- no confirmado contra device real, forma probable.
_VRP_IPV4_SECONDARY = re.compile(r"^\s*ip address\s+(\S+)\s+(\S+)\s+sub\s*$", re.IGNORECASE)
_VRP_IPV4 = re.compile(r"^\s*ip address\s+(\S+)\s+(\S+)\s*$")
# Confirmado contra config real de producción: CIDR de un tirón, igual que
# Cisco -- no separado addr+prefix-length como el IPv4 de esta plataforma.
_VRP_IPV6 = re.compile(r"^\s*ipv6 address\s+(\S+)\s*$")
# El orden de traffic-filter cambia según numerada vs con nombre --
# confirmado contra config real: "traffic-filter inbound acl 3002" para
# numeradas, "traffic-filter acl <nombre> inbound" para ACLs con nombre.
_VRP_ACL_NUMERIC = re.compile(r"^\s*traffic-filter\s+(inbound|outbound)\s+acl\s+(\S+)\s*$", re.IGNORECASE)
_VRP_ACL_NAMED = re.compile(r"^\s*traffic-filter\s+acl\s+(\S+)\s+(inbound|outbound)\s*$", re.IGNORECASE)
# Confirmado contra config real de producción: "dhcp relay server-ip <ip>"
# por cada server (forma directa). La forma alternativa por "server group"
# (huawei/commands.yaml: set_svi_dhcp_relay, 2da alternativa -- la única
# que existe en el CE12800 de lab) no deja ninguna IP en el bloque de la
# interfaz, solo esta línea con el nombre del grupo -- las IPs viven en un
# bloque global aparte, resuelto por _parse_vrp_dhcp_relay_groups() más
# abajo. Bug real encontrado probando esto en vivo: sin este segundo
# camino, reconciliar() veía la interfaz siempre con dhcp_relay_servers=
# None aunque el grupo tuviera servers reales, así que
# SVI._aplicar_dhcp_relay_add() agregaba el 2do server sobre una lista que
# creía vacía y el driver (full-replace puertas adentro) pisaba el 1ro.
_VRP_HELPER = re.compile(r"^\s*dhcp relay server-ip\s+(\S+)\s*$", re.IGNORECASE)
_VRP_BINDING = re.compile(r"^\s*dhcp relay binding server group\s+(\S+)\s*$", re.IGNORECASE)

_VRP_BRIEF_LINE = re.compile(
    r"^Vlanif(\d+)\s+(up|down|\*down)\s+(up|down)\s*", re.IGNORECASE,
)

# "display current-configuration configuration dhcp" -- confirmado contra
# el device real de lab: cada grupo es un bloque separado por líneas "#",
# con el nombre en el header y 1 "server {ip} {index}" por línea.
_VRP_GROUP_HEADER = re.compile(r"^dhcp relay server group\s+(\S+)\s*$", re.IGNORECASE)
_VRP_GROUP_SERVER = re.compile(r"^\s*server\s+(\S+)\s+\d+\s*$", re.IGNORECASE)


def parse_vrp_dhcp_relay_groups(dhcp_config_output: str) -> dict[str, list[str]]:
    """Parse ``display current-configuration configuration dhcp`` en
    ``{group_name: [server_ip, ...]}`` -- el mapeo que ``parse_vrp_svis()``
    necesita para resolver la forma "server group" del DHCP relay (ver
    nota en ``_VRP_HELPER`` arriba)."""
    groups: dict[str, list[str]] = {}
    current: str | None = None
    for raw in dhcp_config_output.splitlines():
        line = _ANSI_ESCAPE.sub("", raw).rstrip()
        header = _VRP_GROUP_HEADER.match(line)
        if header:
            current = header.group(1)
            groups.setdefault(current, [])
            continue
        if current is None:
            continue
        m = _VRP_GROUP_SERVER.match(line)
        if m:
            groups[current].append(m.group(1))
            continue
        if line.strip() == "#":
            current = None
    return groups


def parse_vrp_svis(
    config_output: str, brief_output: str, dhcp_groups: "dict[str, list[str]] | None" = None,
) -> list[SVI]:
    """Parse ``display current-configuration interface Vlanif`` +
    ``display ip interface brief`` en ``SVI``. Mismo criterio
    de 2 fuentes que Cisco: una para la config deseada, otra para el
    estado administrativo real. *dhcp_groups*, cuando se pasa (ver
    ``parse_vrp_dhcp_relay_groups()``), resuelve la forma "server group"
    del DHCP relay -- ver nota en ``_VRP_HELPER``."""
    dhcp_groups = dhcp_groups or {}
    estado_por_vlan: dict[int, tuple[bool, bool]] = {}
    for raw in brief_output.splitlines():
        line = _ANSI_ESCAPE.sub("", raw).rstrip()
        m = _VRP_BRIEF_LINE.match(line)
        if not m:
            continue
        vlan_id, physical, protocol = int(m.group(1)), m.group(2), m.group(3)
        admin_up = not physical.lower().startswith("*")  # "*down" = administrativamente abajo
        operational_up = protocol.lower() == "up"
        estado_por_vlan[vlan_id] = (admin_up, operational_up)

    interfaces: list[SVI] = []
    actual: SVI | None = None
    helpers: list[str] = []
    binding_group: str | None = None

    def _cerrar_actual() -> None:
        if actual is not None:
            if binding_group is not None:
                actual.dhcp_relay_servers = dhcp_groups.get(binding_group) or None
            else:
                actual.dhcp_relay_servers = helpers[:] if helpers else None
            interfaces.append(actual)

    for raw in config_output.splitlines():
        line = _ANSI_ESCAPE.sub("", raw).rstrip()
        header = _VRP_IFACE_HEADER.match(line)
        if header:
            _cerrar_actual()
            vlan_id = int(header.group(1))
            admin_up, operational_up = estado_por_vlan.get(vlan_id, (None, None))
            actual = SVI(vlan_id=vlan_id, admin_up=admin_up, operational_up=operational_up)
            helpers = []
            binding_group = None
            continue
        if actual is None:
            continue
        if _VRP_SHUTDOWN.match(line):
            actual.admin_up = False
            continue
        m = _VRP_DESCRIPTION.match(line)
        if m:
            actual.description = m.group(1)
            continue
        m = _VRP_IPV4_SECONDARY.match(line)
        if m:
            actual.ipv4_address_secondary = _direccion_y_mascara_a_cidr(m.group(1), m.group(2))
            continue
        m = _VRP_IPV4.match(line)
        if m:
            actual.ipv4_address = _direccion_y_mascara_a_cidr(m.group(1), m.group(2))
            continue
        m = _VRP_IPV6.match(line)
        if m:
            actual.ipv6_address = _normalizar_ipv6(m.group(1))
            continue
        m = _VRP_ACL_NUMERIC.match(line)
        if m:
            if m.group(1).lower() == "inbound":
                actual.acl_in = m.group(2)
            else:
                actual.acl_out = m.group(2)
            continue
        m = _VRP_ACL_NAMED.match(line)
        if m:
            if m.group(2).lower() == "inbound":
                actual.acl_in = m.group(1)
            else:
                actual.acl_out = m.group(1)
            continue
        m = _VRP_HELPER.match(line)
        if m:
            helpers.append(m.group(1))
            continue
        m = _VRP_BINDING.match(line)
        if m:
            binding_group = m.group(1)
            continue

    _cerrar_actual()
    return interfaces


# ── Listado de ACLs (RF-INTERV-04's precondición "ACL previamente creada") ──

# "Standard IP access list <nombre-o-número>" / "Extended IP access list
# <nombre-o-número>" -- confirmado contra el device real de lab. Un nombre
# "(per-user)" trae un sufijo tras un espacio, \S+ ya corta antes de eso.
_IOS_ACL_HEADER = re.compile(r"^(?:Standard|Extended) IP access list (\S+)", re.IGNORECASE)


def parse_ios_acl_names(show_access_lists_output: str) -> list[str]:
    return [
        m.group(1) for raw in show_access_lists_output.splitlines()
        if (m := _IOS_ACL_HEADER.match(_ANSI_ESCAPE.sub("", raw).rstrip()))
    ]


# Confirmado contra config real de producción, ambos casos: ACLs numeradas
# ("Advanced ACL 3000, 8 rules") y con nombre ("Basic Name ACL
# acceso-snmp, 2 rules" -- la palabra "Name" de más solo aparece cuando la
# ACL tiene nombre, no número). "Total nonempty ACL number is 0" (caso
# vacío) confirmado aparte contra el device real de lab.
_VRP_ACL_HEADER = re.compile(
    r"^(?:Basic|Advanced|Ethernet frame|User)\s+(?:Name\s+)?ACL\s+(\S+?),", re.IGNORECASE,
)


def parse_vrp_acl_names(display_acl_all_output: str) -> list[str]:
    return [
        m.group(1) for raw in display_acl_all_output.splitlines()
        if (m := _VRP_ACL_HEADER.match(_ANSI_ESCAPE.sub("", raw).rstrip()))
    ]
