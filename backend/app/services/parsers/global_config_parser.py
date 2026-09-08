"""Parsers para "Configuración Global" (SRS §3.4, RF-GLOBAL-01 a 04 --
lectura). Mismo patrón base/subclase que ``port_parser.py``/``svi_parser.py``
(``VendorDriver``/``CiscoVendor``/``HuaweiVendor``).

Confirmado contra los 4 devices reales de esta sesión (``cisco01``/
``huawei01`` de lab, ``f3r9s1``/``f3r9s2`` reales): ``show version``/
``display version``, hostname vía grep de running-config, y la tabla de
ruteo. **Sin confirmar todavía**: el formato exacto de la línea de
community SNMP en VRP (ningún device Huawei de esta sesión tiene SNMP
configurado para poder capturarla) -- ver nota en
``HuaweiGlobalConfigParser``.
"""
from __future__ import annotations

import ipaddress
import re
from abc import ABC, abstractmethod

from app.models.global_config import GlobalConfig
from app.services.parsers._common import strip_ansi


class GlobalConfigParser(ABC):
    """Base -- ver docstring de módulo y el mismo patrón en
    ``PortParser``/``SVIParser``."""

    @classmethod
    @abstractmethod
    def parse(cls, **raw_outputs: str) -> GlobalConfig:
        """Combina el output de este vendor en un ``GlobalConfig``. Firma
        exacta (qué kwargs) es vendor-específica -- ver cada subclase."""
        ...


# ── Cisco IOS ────────────────────────────────────────────────────────────────

_IOS_HOSTNAME_LINE = re.compile(r"^hostname\s+(\S+)\s*$", re.IGNORECASE)
_IOS_SNMP_COMMUNITY_LINE = re.compile(
    r"^snmp-server community\s+(\S+)\s+(RO|RW)\b", re.IGNORECASE,
)
# "C        10.10.10.0/24 is directly connected, Vlan10" (conectada) /
# "S     192.168.99.0/24 [1/0] via 10.10.10.99" (estática/vía next-hop) --
# ambos confirmados en vivo contra cisco01 (RF-GLOBAL-06: agregar una ruta
# estática nueva sin uptime/AD-suffix reveló que el ",?\s" final de la
# versión anterior de este regex nunca matcheaba una ruta recién agregada
# -- el next-hop termina la línea directo, sin espacio/coma detrás; el
# formato CON sufijo (", 00:01:23") es el documentado para rutas con más
# tiempo activas, sigue soportado por el "?" de "(?:\s|$)").
_IOS_ROUTE_CONNECTED_RE = re.compile(
    r"^[A-Z*]{1,3}\s+(\S+/\d+)\s+is directly connected,\s+(\S+)", re.IGNORECASE,
)
_IOS_ROUTE_VIA_RE = re.compile(
    r"^[A-Z*]{1,3}\s+(\S+/\d+)\s+\[\d+/\d+\]\s+via\s+(\S+?),?(?:\s|$)", re.IGNORECASE,
)
# "ip route 192.168.100.0 255.255.255.0 10.10.100.10" -- leído de
# running-config, NO de la RIB. Complementa (no reemplaza) el parse de
# arriba: una ruta estática con next-hop no alcanzable desde este device
# queda en el config pero JAMÁS se instala en la RIB (confirmado en vivo
# contra f3r9s1, ver el docstring de ``_aplicar_route_remove`` en
# ``global_config.py`` -- ese límite quedaba documentado como "fuera de
# alcance"; esto lo cierra). Sin este regex esas rutas eran invisibles
# para la app enTERA (no aparecían para poder borrarlas ni para detectar
# que ya existían) -- ese era justo el pedido del usuario, que se vean
# aunque no estén activas para poder borrarlas desde la interfaz.
_IOS_STATIC_ROUTE_RE = re.compile(
    r"^ip route\s+(\S+)\s+(\S+)\s+(\S+)", re.IGNORECASE | re.MULTILINE,
)
# NTP/DNS/log server+level -- confirmado en vivo contra f3r9s1, todos
# leídos del mismo running-config completo que ya se trae para
# RF-GLOBAL-01 (no hace falta ningún comando nuevo). ``findall`` -- puede
# haber más de 1 configurado, se devuelve la lista completa en el orden en
# que aparecen (ver ``ntp_server_add``/``dns_server_add`` para el alta
# incremental, sin no-op detection contra esta lista todavía). Cisco no
# tiene un ajuste de "versión SNMP" independiente de la community (ver
# ``set_snmp`` en el driver) -- no hay regex de eso acá a propósito.
_IOS_NTP_SERVER_RE = re.compile(r"^ntp server\s+(\S+)", re.IGNORECASE | re.MULTILINE)
# "ip name-server" acepta hasta 6 IPs en la MISMA línea separadas por
# espacio (confirmado en vivo contra f3r9s1: "ip name-server 172.16.16.80
# 200.16.16.3", 2 servers en 1 sola línea -- a diferencia de "ntp server"/
# "logging host" que son 1 destino por comando/línea). El grupo captura el
# resto de la línea entera, no solo el primer token -- se separa por
# espacios en ``parse()``.
_IOS_DNS_SERVER_RE = re.compile(r"^ip name-server\s+(.+)$", re.IGNORECASE | re.MULTILINE)
_IOS_LOG_HOST_RE = re.compile(r"^logging host\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_IOS_LOG_LEVEL_RE = re.compile(r"^logging trap\s+(\S+)", re.IGNORECASE | re.MULTILINE)
# IOS clásico (community-based) no tiene un ajuste de "versión SNMP"
# independiente -- pero SI hay trap-host(s) configurado(s) (RF-GLOBAL-07,
# ``snmp-server host {ip} version {v} {community}``), esas IPs y su
# versión son lo más parecido a "la versión/hosts SNMP en uso" que se
# puede leer. Confirmado en vivo contra f3r9s1 (que ya tenía un trap-host
# real configurado): "snmp-server host 172.19.19.46 version 2c
# unyberzydadez". ``findall`` -- puede haber más de 1 línea (varios
# trap-hosts), se devuelven todos (mismo criterio que Huawei, que expone
# TODOS los hosts permitidos por su ACL, no solo el primero). La versión
# sigue siendo 1 solo valor (se toma la del primer match) -- no tiene
# sentido "más de 1 versión SNMP en uso" a diferencia de los hosts.
_IOS_SNMP_TRAP_HOST_VERSION_RE = re.compile(
    r"^snmp-server host\s+(\S+)\s+version\s+(\S+)", re.IGNORECASE | re.MULTILINE,
)


class CiscoGlobalConfigParser(GlobalConfigParser):
    @classmethod
    def parse(
        cls, *, version_output: str = "", hostname_output: str = "",
        community_output: str = "", route_output: str = "",
        running_config_output: str = "", snmp_enabled: bool | None = None,
    ) -> GlobalConfig:
        hostname = None
        for raw in hostname_output.splitlines():
            m = _IOS_HOSTNAME_LINE.match(strip_ansi(raw).rstrip())
            if m:
                hostname = m.group(1)
                break

        snmp_community = snmp_permission = None
        for raw in community_output.splitlines():
            m = _IOS_SNMP_COMMUNITY_LINE.match(strip_ansi(raw).rstrip())
            if m:
                snmp_community, snmp_permission = m.group(1), m.group(2).upper()
                break

        rc = strip_ansi(running_config_output)

        routes: list[dict] = []
        routes_por_clave: dict[tuple[str, str | None], dict] = {}
        for raw in route_output.splitlines():
            line = strip_ansi(raw).rstrip()
            m = _IOS_ROUTE_CONNECTED_RE.match(line)
            if m:
                ruta = {"destination": m.group(1), "next_hop": None, "interface": m.group(2)}
                routes.append(ruta)
                routes_por_clave[(ruta["destination"], ruta["next_hop"])] = ruta
                continue
            m = _IOS_ROUTE_VIA_RE.match(line)
            if m:
                ruta = {"destination": m.group(1), "next_hop": m.group(2), "interface": None}
                routes.append(ruta)
                routes_por_clave[(ruta["destination"], ruta["next_hop"])] = ruta
        for dest, mask, next_hop in _IOS_STATIC_ROUTE_RE.findall(rc):
            try:
                destino = str(ipaddress.ip_network(f"{dest}/{mask}", strict=False))
            except ValueError:
                continue
            clave = (destino, next_hop)
            if clave not in routes_por_clave:
                ruta = {"destination": destino, "next_hop": next_hop, "interface": None}
                routes.append(ruta)
                routes_por_clave[clave] = ruta

        version_text = strip_ansi(version_output).strip() or None
        ntp_servers = _IOS_NTP_SERVER_RE.findall(rc)
        dns_servers = [ip for linea in _IOS_DNS_SERVER_RE.findall(rc) for ip in linea.split()]
        log_servers = _IOS_LOG_HOST_RE.findall(rc)
        m_log_level = _IOS_LOG_LEVEL_RE.search(rc)
        snmp_traps = _IOS_SNMP_TRAP_HOST_VERSION_RE.findall(rc)

        return GlobalConfig(
            hostname=hostname,
            device_version=version_text,
            snmp_enabled=snmp_enabled,
            snmp_community=snmp_community,
            snmp_permission=snmp_permission,
            snmp_version=snmp_traps[0][1] if snmp_traps else None,
            snmp_trap_hosts=[host for host, _version in snmp_traps] or None,
            ntp_servers=ntp_servers or None,
            dns_servers=dns_servers or None,
            log_servers=log_servers or None,
            log_level=m_log_level.group(1) if m_log_level else None,
            routes=routes or None,
        )


# ── Huawei VRP ───────────────────────────────────────────────────────────────

_VRP_HOSTNAME_LINE = re.compile(r"^sysname\s+(\S+)\s*$", re.IGNORECASE)
# Confirmado contra f3r9s2/huawei01 reales: "Destination/Mask Proto Pre Cost
# Flags NextHop Interface", separado por espacios (destino right-aligned,
# ancho variable). VRP trunca el nombre de interfaz cuando la tabla es
# ancha (confirmado, ver ejemplo real "...172.99.99.1     Vla" en vez de
# "Vlanif..." completo) -- no hay forma de arreglar esto del lado del
# parser, es el device el que corta la columna.
_VRP_ROUTE_LINE = re.compile(
    r"^\s*(\S+/\d+)\s+(\S+)\s+(\d+)\s+(\d+)\s+\S*\s+(\S+)\s+(\S+)\s*$",
)
# "ip route-static 192.168.100.0 255.255.255.0 10.10.100.10" -- misma
# lógica que ``_IOS_STATIC_ROUTE_RE`` (ver comentario ahí): complementa el
# parse de la tabla de ruteo activa con las rutas estáticas del config,
# para que una con next-hop no alcanzable siga siendo visible/borrable.
_VRP_STATIC_ROUTE_RE = re.compile(
    r"^ip route-static\s+(\S+)\s+(\S+)\s+(\S+)", re.IGNORECASE | re.MULTILINE,
)
# NTP/DNS/log server+level/snmp version -- confirmado en vivo contra
# f3r9s2, todos leídos del mismo running-config completo que ya se trae
# para RF-GLOBAL-01. "ntp(?:-service)? unicast-server" cubre las 2 formas
# reales confirmadas esta sesión: huawei01 (CE12800 lab) usa "ntp
# unicast-server" sin "-service", f3r9s2 (S-series real) usa "ntp-service
# unicast-server" CON "-service" -- mismo comando funcionalmente, distinto
# nombre según familia de plataforma (igual que ya pasaba con el nombre de
# interfaz abreviado/completo). "snmp-agent sys-info version" puede traer
# más de 1 versión habilitada a la vez ("v2c v3", aditivo -- ver
# ``set_snmp`` en el driver) -- se guarda tal cual, sin partirlo. NTP/DNS/
# log usan ``findall`` -- puede haber más de 1 configurado, se devuelve la
# lista completa (mismo criterio que Cisco).
_VRP_NTP_SERVER_RE = re.compile(r"^ntp(?:-service)?\s+unicast-server\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_VRP_DNS_SERVER_RE = re.compile(r"^dns server\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_VRP_LOG_HOST_RE = re.compile(r"^info-center loghost\s+(\S+)", re.IGNORECASE | re.MULTILINE)
_VRP_LOG_LEVEL_RE = re.compile(
    r"^info-center source default channel\s+\d+\s+log level\s+(\S+)", re.IGNORECASE | re.MULTILINE,
)
_VRP_SNMP_VERSION_RE = re.compile(r"^snmp-agent sys-info version\s+(.+)$", re.IGNORECASE | re.MULTILINE)
# "snmp-agent community read cipher %^%#..." -- la community en sí queda
# cifrada (ver docstring de ``parse()``), pero el verbo read/write NO está
# cifrado, así que el permiso sí se puede leer aunque la community no.
# Confirmado en vivo contra f3r9s2.
_VRP_SNMP_PERMISSION_RE = re.compile(r"^snmp-agent community\s+(read|write)\b", re.IGNORECASE | re.MULTILINE)
# "snmp-agent acl acceso-snmp" -- confirmado en vivo contra f3r9s2: VRP no
# tiene un "trap-host" leíble (ver nota de ``target-host`` en
# ``HuaweiVendor.set_snmp()``), pero SÍ tiene esta línea que ata la
# community/el agente SNMP a una ACL de hosts permitidos -- el usuario
# señaló que esto es lo que hay que usar para poblar ``snmp_trap_hosts``
# acá (no son "trap destinations" reales como en Cisco, son "los hosts de
# management permitidos para SNMP", pero cumple el mismo rol de
# visibilidad -- TODOS los permit de la ACL, no solo el primero, pedido
# explícito del usuario). Requiere cruzar esta ACL contra ``acls`` (ya
# trae las reglas, ver ``list_acls()``) -- se resuelve en ``parse()``, no
# acá. El nombre de la ACL en sí se guarda aparte en
# ``GlobalConfig.snmp_acl_name`` (transiente, lo usa ``_aplicar_snmp_config()``
# para saber a qué ACL agregarle una regla al escribir un ``trap_host``
# nuevo, ver ``HuaweiVendor.set_snmp()``).
_VRP_SNMP_ACL_RE = re.compile(r"^snmp-agent acl\s+(\S+)", re.IGNORECASE | re.MULTILINE)
# "rule 5 permit source 172.19.19.46 0" -- el "0" final es la wildcard
# mask, no la IP, se descarta.
_VRP_ACL_RULE_SOURCE_IP_RE = re.compile(r"^rule\s+\d+\s+permit\s+source\s+(\S+)", re.IGNORECASE)


class HuaweiGlobalConfigParser(GlobalConfigParser):
    @classmethod
    def parse(
        cls, *, version_output: str = "", hostname_output: str = "",
        route_output: str = "", running_config_output: str = "", snmp_enabled: bool | None = None,
        acls: list[dict] | None = None,
    ) -> GlobalConfig:
        """A diferencia de Cisco, la community SNMP en sí queda siempre en
        ``None`` acá -- confirmado en vivo esta sesión que VRP la guarda
        cifrada (``snmp-agent community read cipher <...>``), no hay forma
        de revertir el cifrado del lado del parser (limitación real del
        vendor, no un gap sin confirmar). El PERMISO (read/write) sí se
        puede leer, ver ``_VRP_SNMP_PERMISSION_RE``. ``snmp_trap_hosts`` acá
        NO viene de un comando de trap-host real (``target-host`` no tiene
        lectura implementada, ver nota en ``HuaweiVendor.set_snmp()``) sino
        de la ACL que ``snmp-agent acl {nombre}`` ata al agente SNMP -- se
        toman TODOS los ``permit source`` de esa ACL (necesita ``acls`` ya
        resuelto con reglas, ver ``list_acls()``; si no se pasa o la ACL no
        aparece ahí, queda ``None``)."""
        hostname = None
        for raw in hostname_output.splitlines():
            m = _VRP_HOSTNAME_LINE.match(strip_ansi(raw).rstrip())
            if m:
                hostname = m.group(1)
                break

        rc = strip_ansi(running_config_output)

        routes: list[dict] = []
        routes_por_clave: dict[tuple[str, str | None], dict] = {}
        for raw in route_output.splitlines():
            line = strip_ansi(raw).rstrip()
            m = _VRP_ROUTE_LINE.match(line)
            if m and "/" in m.group(1):
                ruta = {
                    "destination": m.group(1), "next_hop": m.group(5), "interface": m.group(6),
                }
                routes.append(ruta)
                routes_por_clave[(ruta["destination"], ruta["next_hop"])] = ruta
        for dest, mask, next_hop in _VRP_STATIC_ROUTE_RE.findall(rc):
            try:
                destino = str(ipaddress.ip_network(f"{dest}/{mask}", strict=False))
            except ValueError:
                continue
            clave = (destino, next_hop)
            if clave not in routes_por_clave:
                ruta = {"destination": destino, "next_hop": next_hop, "interface": None}
                routes.append(ruta)
                routes_por_clave[clave] = ruta

        version_text = strip_ansi(version_output).strip() or None
        ntp_servers = _VRP_NTP_SERVER_RE.findall(rc)
        dns_servers = _VRP_DNS_SERVER_RE.findall(rc)
        log_servers = _VRP_LOG_HOST_RE.findall(rc)
        m_log_level = _VRP_LOG_LEVEL_RE.search(rc)
        m_snmp_version = _VRP_SNMP_VERSION_RE.search(rc)
        m_snmp_permission = _VRP_SNMP_PERMISSION_RE.search(rc)
        snmp_permission = {"read": "RO", "write": "RW"}.get(m_snmp_permission.group(1).lower()) if m_snmp_permission else None

        snmp_trap_hosts = None
        acl_name = None
        m_snmp_acl = _VRP_SNMP_ACL_RE.search(rc)
        if m_snmp_acl:
            acl_name = m_snmp_acl.group(1)
            if acls:
                acl = next((a for a in acls if a.get("name") == acl_name), None)
                if acl is not None:
                    hosts = [
                        m_ip.group(1) for regla in acl.get("rules", [])
                        if (m_ip := _VRP_ACL_RULE_SOURCE_IP_RE.match(regla))
                    ]
                    snmp_trap_hosts = hosts or None

        return GlobalConfig(
            hostname=hostname,
            device_version=version_text,
            snmp_enabled=snmp_enabled,
            snmp_community=None,
            snmp_permission=snmp_permission,
            snmp_version=m_snmp_version.group(1) if m_snmp_version else None,
            snmp_trap_hosts=snmp_trap_hosts,
            snmp_acl_name=acl_name,
            ntp_servers=ntp_servers or None,
            dns_servers=dns_servers or None,
            log_servers=log_servers or None,
            log_level=m_log_level.group(1) if m_log_level else None,
            routes=routes or None,
        )
