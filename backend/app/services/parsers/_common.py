"""Utilidades compartidas por todos los parsers de este paquete -- antes
``_ANSI_ESCAPE`` vivía duplicado (idéntico) en ``vlan_parser.py``,
``svi_parser.py`` y ``port_parser.py``."""
from __future__ import annotations

import re

ANSI_ESCAPE = re.compile(r"\x1B\[[0-9;]*m")


def strip_ansi(line: str) -> str:
    """Remove ANSI colour/cursor escape codes from *line*."""
    return ANSI_ESCAPE.sub("", line)
