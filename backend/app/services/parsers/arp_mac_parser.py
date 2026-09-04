"""Parsers para las tablas ARP y MAC (fuera de RF-GLOBAL-01..09, pedido del
usuario tras ver el output crudo de ``GET .../arp``/``.../mac`` -- "puede
llegar a ser mucha info" y sin estructura es imposible de tabular en el
frontend sin duplicar el parseo ahí). Todo confirmado en vivo contra
f3r9s1 (Cisco IOS-XE real) y f3r9s2 (Huawei VRP S-series real) -- sin lab
disponible esta vuelta, así que **sin confirmar todavía** contra un IOS
clásico o un VRP CE-series (formato puede variar, ver notas de sesión sobre
cuánto varía ``show version`` incluso dentro del mismo vendor).
"""
from __future__ import annotations

import re

from app.services.parsers._common import strip_ansi

_IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")

# ── Cisco IOS ────────────────────────────────────────────────────────────────

# "Internet  172.16.60.1             0   4c00.829e.117f  ARPA   Vlan156" --
# confirmado en vivo contra f3r9s1, 5/5 filas reales parseadas.
_CISCO_ARP_RE = re.compile(r"^Internet\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$")
# " 156    0056.2b0f.996c    DYNAMIC     Gi1/0/24" / " All    0100.0ccc.cccc
# STATIC      CPU" -- el mac (3 grupos de 4 hex separados por ".") ancla la
# fila real y descarta headers/separadores sin necesidad de saltear líneas a
# mano. Confirmado en vivo: 49/49 filas reales parseadas (incluye las "All"
# de multicast/protocolo reservado, no solo las dinámicas por-VLAN).
_CISCO_MAC_RE = re.compile(r"^\s*(\S+)\s+(\S+\.\S+\.\S+)\s+(\S+)\s+(\S+)\s*$")


def parse_cisco_arp(raw: str) -> list[dict]:
    entradas = []
    for linea in raw.splitlines():
        m = _CISCO_ARP_RE.match(strip_ansi(linea).rstrip())
        if m:
            ip, age, mac, tipo, interface = m.groups()
            entradas.append({"ip": ip, "mac": mac, "age": age, "type": tipo, "interface": interface, "vlan": None})
    return entradas


def parse_cisco_mac(raw: str) -> list[dict]:
    entradas = []
    for linea in raw.splitlines():
        m = _CISCO_MAC_RE.match(strip_ansi(linea).rstrip())
        if m:
            vlan, mac, tipo, interface = m.groups()
            entradas.append({"mac": mac, "vlan": vlan, "type": tipo, "interface": interface})
    return entradas


# ── Huawei VRP ───────────────────────────────────────────────────────────────

# "172.16.61.126   a8b4-569f-e451  16        D-0         GE0/0/47" (dinámica,
# con línea de continuación "                                           156/-"
# para el VLAN) / "172.16.61.210   c8b6-d316-5647            I -         Vlanif156"
# (entrada propia de una interfaz local, sin continuación) -- ambas SIEMPRE
# tokenizan a exactamente 5 campos (ip, mac, age-o-marcador, type, interface),
# aunque para el caso "I -" el 3er/4to campo no sean un expire/type real en
# el sentido literal (así los expone el device, se documenta tal cual sin
# inventarles otro significado). Confirmado en vivo: 44/44 filas reales
# parseadas, matchea el "Total:44" que reporta el propio device.
_VRP_VLAN_CONTINUATION_RE = re.compile(r"^\s*(\d+)/")

# "0056-2b0f-996c 156/-/-                           GE0/0/48            dynamic"
# -- 1 sola línea, sin ambigüedad. Confirmado en vivo: 52/52 filas reales
# parseadas, matchea el "Total items displayed = 52" del propio device.
_HUAWEI_MAC_RE = re.compile(r"^(\S{4}-\S{4}-\S{4})\s+(\S+)\s+(\S+)\s+(\S+)\s*$")


def parse_huawei_arp(raw: str) -> list[dict]:
    lineas = [strip_ansi(l).rstrip() for l in raw.splitlines()]
    entradas = []
    i, n = 0, len(lineas)
    while i < n:
        tokens = lineas[i].split()
        if len(tokens) == 5 and _IP_RE.match(tokens[0]):
            ip, mac, age, tipo, interface = tokens
            vlan = None
            if i + 1 < n:
                m = _VRP_VLAN_CONTINUATION_RE.match(lineas[i + 1])
                if m:
                    vlan = m.group(1)
                    i += 1  # la continuación ya se consumió, no procesarla de nuevo
            entradas.append({"ip": ip, "mac": mac, "age": age, "type": tipo, "interface": interface, "vlan": vlan})
        i += 1
    return entradas


def parse_huawei_mac(raw: str) -> list[dict]:
    entradas = []
    for linea in raw.splitlines():
        m = _HUAWEI_MAC_RE.match(strip_ansi(linea).rstrip())
        if m:
            mac, vlan_vsi_bd, interface, tipo = m.groups()
            entradas.append({"mac": mac, "vlan": vlan_vsi_bd.split("/")[0], "type": tipo, "interface": interface})
    return entradas
