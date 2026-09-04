"""Utilidades compartidas por todos los parsers de este paquete -- antes
``_ANSI_ESCAPE`` vivía duplicado (idéntico) en ``vlan_parser.py``,
``svi_parser.py`` y ``port_parser.py``."""
from __future__ import annotations

import re

ANSI_ESCAPE = re.compile(r"\x1B\[[0-9;]*m")


def strip_ansi(line: str) -> str:
    """Remove ANSI colour/cursor escape codes from *line*."""
    return ANSI_ESCAPE.sub("", line)


def strip_known_preamble(text: str, patterns: "list[str]", separador: "str | None" = None) -> str:
    """RF-GLOBAL-01 -- ``show running-config``/``display
    current-configuration`` empiezan con líneas de metadata que no son
    config real (``"Building configuration..."``, ``"Current
    configuration : N bytes"``, ``"! Last configuration change..."`` en
    IOS; ``"!Software Version ..."`` en VRP) -- el usuario pidió que el
    endpoint de config muestre "solo la config del equipo". Saca SOLO las
    líneas iniciales que matchean *patterns* (+ blancos sueltos al
    principio), no toca nada del resto.

    *separador* (ej. ``"!"`` en IOS) es opcional -- IOS intercala líneas de
    separador SUELTAS entre los comentarios de metadata (confirmado en vivo
    contra f3r9s1: ``"!"`` / ``"! Last configuration change..."`` / ``"!
    NVRAM config last updated..."`` / ``"!"`` / ``"version 16.6"``), así que
    sin esto el corte se frena en el primer separador suelto y nunca llega
    a los comentarios de después. Solo afecta el PREFIJO inicial -- un
    separador real más adelante en el config (la inmensa mayoría) nunca se
    toca."""
    lineas = text.splitlines()
    compilados = [re.compile(p) for p in patterns]
    while lineas and (
        lineas[0].strip() == ""
        or (separador is not None and lineas[0].strip() == separador)
        or any(p.match(lineas[0]) for p in compilados)
    ):
        lineas.pop(0)
    return "\n".join(lineas)


_UPTIME_RE = re.compile(r"uptime is ([^\r\n]+)")
# Solo se busca en las primeras _VERSION_SCAN_LINES líneas -- "Version X"
# aparece ahí en los 2 formatos confirmados en vivo (IOS clásico/IOS-XE:
# línea 1; VRP: línea 2), y evita matchear menciones tardías irrelevantes
# ("GPL... Version 2.0", "BOOTLDR... Version 3.56") que sí aparecen más
# abajo en el mismo output.
_VERSION_SCAN_LINES = 5
_VERSION_STRING_RE = re.compile(r"Version\s+([0-9][\w.]*)")
# "Model Number                       : WS-C3650-24TD" (IOS-XE, confirmado
# contra f3r9s1) -- IOS clásico (cisco01 de lab) no tiene esta línea, queda
# None ahí.
_CISCO_MODEL_RE = re.compile(r"^Model Number\s*:\s*(.+)$", re.MULTILINE)
_CISCO_SERIAL_RE = re.compile(r"^System Serial Number\s*:\s*(.+)$", re.MULTILINE)
# "HUAWEI S5731-H48T4XC Routing Switch uptime is..." / "HUAWEI CE12800
# uptime is..." -- confirmado contra f3r9s2/huawei01 reales, el modelo va
# pegado después de "HUAWEI " en la misma línea del uptime.
_HUAWEI_MODEL_RE = re.compile(r"^HUAWEI\s+(\S+)", re.MULTILINE)


def parse_version_info(raw: str) -> dict:
    """RF-GLOBAL-01 (mitad "versión") -- extrae campos best-effort de
    ``show version``/``display version`` para que el endpoint de versión
    no sea solo un blob de texto. El formato de este comando varía MUCHO
    incluso dentro del mismo vendor (confirmado en vivo: IOS clásico de
    lab vs. IOS-XE de f3r9s1 no comparten ni el layout ni todos los
    campos) -- por eso todo acá es best-effort (``None`` si no matchea) y
    ``raw`` siempre viaja completo, nada se pierde."""
    resultado = {"raw": raw, "software_version": None, "model": None, "serial_number": None, "uptime": None}
    if not raw:
        return resultado

    m = _UPTIME_RE.search(raw)
    if m:
        resultado["uptime"] = m.group(1).strip()

    primeras_lineas = "\n".join(raw.splitlines()[:_VERSION_SCAN_LINES])
    m = _VERSION_STRING_RE.search(primeras_lineas)
    if m:
        resultado["software_version"] = m.group(1)

    m = _CISCO_MODEL_RE.search(raw)
    if m:
        resultado["model"] = m.group(1).strip()
    else:
        m = _HUAWEI_MODEL_RE.search(raw)
        if m:
            resultado["model"] = m.group(1)

    m = _CISCO_SERIAL_RE.search(raw)
    if m:
        resultado["serial_number"] = m.group(1).strip()

    return resultado
