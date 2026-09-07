"""Parsers para el output real de ``get_svis`` -- RF-INTERV-07.

Igual que el resto de los parsers de este paquete, están escritos contra el
formato de output *documentado* de cada plataforma, no contra una captura
verificada de un device real corriendo estos comandos exactos (a diferencia
de ``vlan_parser.py``/``port_parser.py``, que sí lo están). Marcar
como pendiente de confirmar antes de confiar ciegamente en el parseo --
mismo criterio que el resto de esta sesión con sintaxis no verificada.
"""
from __future__ import annotations

import ipaddress
import re
from abc import ABC, abstractmethod

from app.models.svi import SVI
from app.services.parsers._common import strip_ansi


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


class SVIParser(ABC):
    """Base para los parsers de SVI por vendor -- mismo patrón que
    ``PortParser``/``CiscoPortParser``/``HuaweiPortParser`` (y, más atrás,
    ``VendorDriver``/``CiscoVendor``/``HuaweiVendor``): la limpieza de
    líneas ya vive en un solo lugar (``_common.strip_ansi``, no acá
    específicamente); cada subclase implementa su propio ``parse_svis()``
    con sus regex/columnas reales, confirmadas contra devices reales, no
    genéricas -- el escaneo de bloque de config difiere lo suficiente
    entre plataformas (Huawei resuelve DHCP relay contra un mapeo de
    grupos aparte, Cisco no) que forzar un template compartido ahí
    generaría más ceremonia que código evitado."""

    @classmethod
    @abstractmethod
    def parse_svis(cls, config_output: str, brief_output: str, **kwargs) -> list[SVI]:
        """Combina el output de config + brief de este vendor en una lista
        de ``SVI``. Firma exacta (kwargs extra) es vendor-específica --
        ver cada subclase."""
        ...


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
# 'show ip interface brief'. El estado admin real está en la columna
# "Status" (protocol es la de más a la derecha del todo). La columna
# "Method" (4ta, entre OK? y Status) se dejaba como alternación fija
# (manual/dhcp/other/unset) -- bug real encontrado contra un device real:
# esa columna reportaba "NVRAM" (config guardada, no volátil), un valor
# real y válido no cubierto por esa lista, y la línea entera no matcheaba
# nada -- admin_up/operational_up quedaban None para toda VLAN del
# device. Generalizado a \S+ (cualquier token), ya que el valor de esa
# columna no importa para lo que este parser necesita.
_IOS_BRIEF_LINE = re.compile(
    r"^Vlan(\d+)\s+\S+\s+\S+\s+\S+\s+(.+?)\s+(up|down)\s*$",
    re.IGNORECASE,
)


class CiscoSVIParser(SVIParser):
    @classmethod
    def parse_svis(cls, running_config_output: str, brief_output: str, **kwargs) -> list[SVI]:
        """Parse ``show running-config | section ^interface Vlan`` +
        ``show ip interface brief | include Vlan`` en ``SVI``.

        El primero trae la config deseada (ip/ipv6, description, ACLs,
        helper-address); el segundo el estado administrativo real (running-config
        no siempre expone ``shutdown``/``no shutdown`` de forma consistente
        entre versiones de IOS)."""
        estado_por_vlan: dict[int, tuple[bool, bool]] = {}
        for raw in brief_output.splitlines():
            line = strip_ansi(raw).rstrip()
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
            line = strip_ansi(raw).rstrip()
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


def parse_ios_svis(running_config_output: str, brief_output: str) -> list[SVI]:
    """Back-compat free-function wrapper — see ``CiscoSVIParser.parse_svis``."""
    return CiscoSVIParser.parse_svis(running_config_output, brief_output)


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
# Confirmado EN VIVO contra f3r9s2 real (ver huawei/driver.py::set_svi_acl
# y huawei/commands.yaml para el detalle de cómo se probó): el orden es
# SIEMPRE "traffic-filter {direction} acl ..." -- lo que cambia entre
# numerada y con nombre es el keyword "name" antes del nombre:
# "traffic-filter inbound acl 3002" (numerada) vs. "traffic-filter inbound
# acl name test-acl" (con nombre). La forma anterior acá ("traffic-filter
# acl <nombre> inbound") era la sintaxis vieja/incorrecta que motivó el
# fix -- nunca hizo match contra una config real, por eso el bug de lectura
# (acl_in/acl_out con nombre siempre volvía None) pasó desapercibido junto
# con el bug de escritura original.
_VRP_ACL_NUMERIC = re.compile(r"^\s*traffic-filter\s+(inbound|outbound)\s+acl\s+(\S+)\s*$", re.IGNORECASE)
_VRP_ACL_NAMED = re.compile(r"^\s*traffic-filter\s+(inbound|outbound)\s+acl\s+name\s+(\S+)\s*$", re.IGNORECASE)
# Confirmado contra config real de producción: "dhcp relay server-ip <ip>"
# por cada server (forma directa). La forma alternativa por "server group"
# (huawei/commands.yaml: set_svi_dhcp_relay, 2da alternativa -- la única
# que existe en el CE12800 de lab) no deja ninguna IP en el bloque de la
# interfaz, solo esta línea con el nombre del grupo -- las IPs viven en un
# bloque global aparte, resuelto por ``HuaweiSVIParser.parse_dhcp_relay_groups()``
# más abajo. Bug real encontrado probando esto en vivo: sin este segundo
# camino, reconciliar() veía la interfaz siempre con dhcp_relay_servers=
# None aunque el grupo tuviera servers reales, así que
# SVI._aplicar_dhcp_relay_add() agregaba el 2do server sobre una lista que
# creía vacía y el driver (full-replace puertas adentro) pisaba el 1ro.
_VRP_HELPER = re.compile(r"^\s*dhcp relay server-ip\s+(\S+)\s*$", re.IGNORECASE)
_VRP_BINDING = re.compile(r"^\s*dhcp relay binding server group\s+(\S+)\s*$", re.IGNORECASE)

# Bug real encontrado contra un device real: la regex no dejaba lugar para
# la columna "IP Address/Mask" entre el nombre de interfaz y Physical/
# Protocol ("Vlanif156  172.16.61.210/22  up  up") -- nunca matcheaba nada,
# admin_up/operational_up quedaban None para toda SVI del device (incluido
# el CE12800 de lab, que tiene la misma columna -- este bug no era nuevo,
# solo nunca se había notado).
_VRP_BRIEF_LINE = re.compile(
    r"^Vlanif(\d+)\s+\S+\s+(up|down|\*down)\s+(up|down)\s*", re.IGNORECASE,
)

# "display current-configuration configuration dhcp" -- confirmado contra
# el device real de lab: cada grupo es un bloque separado por líneas "#",
# con el nombre en el header y 1 "server {ip} {index}" por línea.
_VRP_GROUP_HEADER = re.compile(r"^dhcp relay server group\s+(\S+)\s*$", re.IGNORECASE)
_VRP_GROUP_SERVER = re.compile(r"^\s*server\s+(\S+)\s+\d+\s*$", re.IGNORECASE)


class HuaweiSVIParser(SVIParser):
    @classmethod
    def parse_dhcp_relay_groups(cls, dhcp_config_output: str) -> dict[str, list[str]]:
        """Parse ``display current-configuration configuration dhcp`` en
        ``{group_name: [server_ip, ...]}`` -- el mapeo que ``parse_svis()``
        necesita para resolver la forma "server group" del DHCP relay (ver
        nota en ``_VRP_HELPER`` arriba)."""
        groups: dict[str, list[str]] = {}
        current: str | None = None
        for raw in dhcp_config_output.splitlines():
            line = strip_ansi(raw).rstrip()
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

    @classmethod
    def parse_svis(
        cls, config_output: str, brief_output: str,
        dhcp_groups: "dict[str, list[str]] | None" = None, **kwargs,
    ) -> list[SVI]:
        """Parse ``display current-configuration interface Vlanif`` +
        ``display ip interface brief`` en ``SVI``. Mismo criterio
        de 2 fuentes que Cisco: una para la config deseada, otra para el
        estado administrativo real. *dhcp_groups*, cuando se pasa (ver
        ``parse_dhcp_relay_groups()``), resuelve la forma "server group"
        del DHCP relay -- ver nota en ``_VRP_HELPER``."""
        dhcp_groups = dhcp_groups or {}
        estado_por_vlan: dict[int, tuple[bool, bool]] = {}
        for raw in brief_output.splitlines():
            line = strip_ansi(raw).rstrip()
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
            line = strip_ansi(raw).rstrip()
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
                if m.group(1).lower() == "inbound":
                    actual.acl_in = m.group(2)
                else:
                    actual.acl_out = m.group(2)
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


def parse_vrp_dhcp_relay_groups(dhcp_config_output: str) -> dict[str, list[str]]:
    """Back-compat free-function wrapper — see ``HuaweiSVIParser.parse_dhcp_relay_groups``."""
    return HuaweiSVIParser.parse_dhcp_relay_groups(dhcp_config_output)


def parse_vrp_svis(
    config_output: str, brief_output: str, dhcp_groups: "dict[str, list[str]] | None" = None,
) -> list[SVI]:
    """Back-compat free-function wrapper — see ``HuaweiSVIParser.parse_svis``."""
    return HuaweiSVIParser.parse_svis(config_output, brief_output, dhcp_groups=dhcp_groups)


# ── Listado de ACLs (RF-INTERV-04's precondición "ACL previamente creada") ──
# No forman parte de SVIParser -- filtran nombres de ACL, no arman SVI, y no
# comparten forma con parse_svis() más allá del ANSI-strip ya centralizado.

# "Standard IP access list <nombre-o-número>" / "Extended IP access list
# <nombre-o-número>" -- confirmado contra el device real de lab. Un nombre
# "(per-user)" trae un sufijo tras un espacio, \S+ ya corta antes de eso.
_IOS_ACL_HEADER = re.compile(r"^(?:Standard|Extended) IP access list (\S+)", re.IGNORECASE)


def parse_ios_acl_names(show_access_lists_output: str) -> list[str]:
    return [
        m.group(1) for raw in show_access_lists_output.splitlines()
        if (m := _IOS_ACL_HEADER.match(strip_ansi(raw).rstrip()))
    ]


# Como ``_IOS_ACL_HEADER`` pero con grupos separados para tipo/nombre --
# usado por ``parse_ios_acls()`` (RF-GLOBAL-01/04, contenido completo de
# cada ACL pedido por el usuario tras ver la respuesta plana). Se mantiene
# como regex aparte en vez de tocar ``_IOS_ACL_HEADER`` para no arriesgar
# ``parse_ios_acl_names()`` (ya confirmado en vivo, usado también por
# ``svis.py`` para la precondición "ACL previamente creada").
_IOS_ACL_HEADER_FULL = re.compile(r"^(Standard|Extended) IP access list (\S+)", re.IGNORECASE)


def parse_ios_acls(show_access_lists_output: str) -> list[dict]:
    """Mismo alcance que ``parse_ios_acl_names`` (solo ACLs IPv4, IPv6
    queda fuera) pero devuelve también las reglas de cada una tal cual las
    imprime el device (sin re-estructurar cada regla en source/dest/
    protocolo -- alcance explícitamente pedido: "que se vea el contenido
    de cada una", no una ACL completamente parseada). Cualquier línea sin
    sangría que no matchea un header (ej. "IPv6 access list ...") corta la
    ACL en curso para no atribuirle reglas de otra sección."""
    acls: list[dict] = []
    current: dict | None = None
    for raw in show_access_lists_output.splitlines():
        line = strip_ansi(raw).rstrip()
        if not line.strip():
            continue
        m = _IOS_ACL_HEADER_FULL.match(line)
        if m:
            current = {"name": m.group(2), "type": m.group(1).lower(), "rules": []}
            acls.append(current)
        elif current is not None and line[:1].isspace():
            current["rules"].append(line.strip())
        else:
            current = None
    return acls


# Confirmado contra config real de producción, 3 formatos distintos según
# plataforma/firmware: ACLs numeradas puras ("Advanced ACL 3000, 8 rules"),
# con nombre y la palabra "Name" de más ("Basic Name ACL acceso-snmp, 2
# rules") y con nombre SIN "Name" pero con el número interno de grupo
# pegado atrás ("Basic ACL acceso-snmp 2998, 2 rules" -- confirmado contra
# f3r9s2 real, este formato hacía que el regex viejo (que esperaba 1 solo
# token entre "ACL" y la coma) nunca matcheara nada ahí, así que
# ``acls`` volvía siempre `[]` aunque el device sí tuviera ACLs). El
# primer token entre "ACL" y la coma es siempre el nombre/número real que
# el usuario reconoce; un 2do token (cuando existe) es el ID interno de
# grupo VRP, se descarta. "Total nonempty ACL number is 0" (caso vacío)
# confirmado aparte contra el device real de lab.
_VRP_ACL_HEADER = re.compile(
    r"^(?:Basic|Advanced|Ethernet frame|User)\s+(?:Name\s+)?ACL\s+(.+?),", re.IGNORECASE,
)


def parse_vrp_acl_names(display_acl_all_output: str) -> list[str]:
    nombres = []
    for raw in display_acl_all_output.splitlines():
        m = _VRP_ACL_HEADER.match(strip_ansi(raw).rstrip())
        if m:
            nombres.append(m.group(1).split()[0])
    return nombres


# Como ``_VRP_ACL_HEADER`` pero con grupos separados para tipo/nombre/ID
# interno (descartado) -- usado por ``parse_vrp_acls()``, mismo criterio
# que ``_IOS_ACL_HEADER_FULL`` (regex aparte para no arriesgar
# ``parse_vrp_acl_names()``, ya confirmado en vivo y usado por
# ``svis.py``). Confirmado en vivo contra f3r9s2 real: "Basic ACL
# acceso-snmp 2998, 2 rules" seguido de "Acl's step is 5" (metadata, no es
# una regla) y las reglas mismas ("rule 5 permit source 172.19.19.46 0").
_VRP_ACL_HEADER_FULL = re.compile(
    r"^(Basic|Advanced|Ethernet frame|User)\s+(?:Name\s+)?ACL\s+(\S+)(?:\s+\d+)?,", re.IGNORECASE,
)
_VRP_ACL_RULE_LINE = re.compile(r"^rule\s+\d+\b", re.IGNORECASE)


def parse_vrp_acls(display_acl_all_output: str) -> list[dict]:
    """Mismo alcance que ``parse_vrp_acl_names`` pero con las reglas de
    cada ACL, tal cual las imprime el device (ver docstring de
    ``parse_ios_acls`` -- mismo criterio de "contenido crudo", no
    estructurado). Descarta "Acl's step is N" (metadata de numeración, no
    una regla) y la línea de encabezado global ("Total nonempty ACL
    number is N")."""
    acls: list[dict] = []
    current: dict | None = None
    for raw in display_acl_all_output.splitlines():
        line = strip_ansi(raw).strip()
        if not line:
            continue
        m = _VRP_ACL_HEADER_FULL.match(line)
        if m:
            current = {"name": m.group(2), "type": m.group(1).lower(), "rules": []}
            acls.append(current)
        elif current is not None and _VRP_ACL_RULE_LINE.match(line):
            current["rules"].append(line)
    return acls
