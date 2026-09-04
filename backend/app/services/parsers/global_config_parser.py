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


class CiscoGlobalConfigParser(GlobalConfigParser):
    @classmethod
    def parse(
        cls, *, version_output: str = "", hostname_output: str = "",
        community_output: str = "", route_output: str = "",
        snmp_enabled: bool | None = None,
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

        routes: list[dict] = []
        for raw in route_output.splitlines():
            line = strip_ansi(raw).rstrip()
            m = _IOS_ROUTE_CONNECTED_RE.match(line)
            if m:
                routes.append({"destination": m.group(1), "next_hop": None, "interface": m.group(2)})
                continue
            m = _IOS_ROUTE_VIA_RE.match(line)
            if m:
                routes.append({"destination": m.group(1), "next_hop": m.group(2), "interface": None})

        version_text = strip_ansi(version_output).strip() or None

        return GlobalConfig(
            hostname=hostname,
            device_version=version_text,
            snmp_enabled=snmp_enabled,
            snmp_community=snmp_community,
            snmp_permission=snmp_permission,
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


class HuaweiGlobalConfigParser(GlobalConfigParser):
    @classmethod
    def parse(
        cls, *, version_output: str = "", hostname_output: str = "",
        route_output: str = "", snmp_enabled: bool | None = None,
        snmp_community: str | None = None, snmp_permission: str | None = None,
    ) -> GlobalConfig:
        """*snmp_community*/*snmp_permission* se reciben ya parseados por el
        caller (o ``None``) -- a diferencia de Cisco, el formato real de la
        línea de community VRP no está confirmado contra ningún device de
        esta sesión (ninguno tiene SNMP habilitado), así que no hay regex
        acá todavía. Confirmar contra un device real con SNMP activo antes
        de agregarla."""
        hostname = None
        for raw in hostname_output.splitlines():
            m = _VRP_HOSTNAME_LINE.match(strip_ansi(raw).rstrip())
            if m:
                hostname = m.group(1)
                break

        routes: list[dict] = []
        for raw in route_output.splitlines():
            line = strip_ansi(raw).rstrip()
            m = _VRP_ROUTE_LINE.match(line)
            if m and "/" in m.group(1):
                routes.append({
                    "destination": m.group(1), "next_hop": m.group(5), "interface": m.group(6),
                })

        version_text = strip_ansi(version_output).strip() or None

        return GlobalConfig(
            hostname=hostname,
            device_version=version_text,
            snmp_enabled=snmp_enabled,
            snmp_community=snmp_community,
            snmp_permission=snmp_permission,
            routes=routes or None,
        )
