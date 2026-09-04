from __future__ import annotations

import re

from app.models.vlan import VLAN
from app.services.parsers._common import strip_ansi

# IOS-internal VLANs that are not user-managed
_IOS_INTERNAL = {1, 1002, 1003, 1004, 1005}

# VRP default/management VLANs excluded from user-visible output
_VRP_INTERNAL = {1}

# Tabular format -- 2 variantes reales confirmadas: el CE12800 de lab expone
# "VID  Type  Status  Property  MAC-LRN  STAT  BC  MC  UC  Description" (9
# columnas fijas), un switch real (S-series) expone "VID  Status  Property
# MAC-LRN  Statistics  Description" (5, sin "Type") -- bug real encontrado
# corriendo esto contra el 2do: la regex vieja exigía "Type" y el corte fijo
# en parts[9:] devolvía 0 VLANs (ninguna fila matcheaba, o la descripción
# salía mal recortada). "Type" ahora es opcional, y el ancho de columnas se
# cuenta del propio header en vez de asumir un número fijo -- ver
# _parse_vrp_tabular().
_VRP_DETAIL_HEADER = re.compile(r"^VID\s+(?:Type\s+)?Status\s+Property", re.IGNORECASE)

# Block format (older VRP style): "The information of VLAN 10:" or "VLAN 10:"
_VRP_VLAN_BLOCK = re.compile(r"(?:The information of )?VLAN\s+(\d+)\s*:", re.IGNORECASE)
_VRP_NAME_LINE = re.compile(r"VLAN\s+Name\s*:\s*(.+)", re.IGNORECASE)


def parse_vrp_vlan_display(output: str) -> list[VLAN]:
    """Parse 'display vlan' output from Huawei VRP devices into ``VLAN`` objects.

    Supports two formats emitted by different VRP versions:

    - **Tabular** (S-series, common): table with columns
      ``VID / Type / Status / … / Description``
    - **Block** (older CE/NE style):
      ``The information of VLAN <id>: / VLAN Name: <name>``

    Skips VLAN 1 (default management VLAN on VRP).

    Parameters
    ----------
    output:
        Raw stdout from the Ansible ``get_vlans`` playbook for a VRP device.

    Returns
    -------
    list[VLAN]
        Normalized VLAN entries, one per configured user VLAN.
    """
    clean_lines = [strip_ansi(line) for line in output.splitlines()]

    # Detect tabular format by locating the detail-table header line
    detail_start = next(
        (i for i, line in enumerate(clean_lines) if _VRP_DETAIL_HEADER.match(line.strip())),
        None,
    )
    if detail_start is not None:
        # Contar las columnas del propio header (menos "Description", que es
        # texto libre de ancho variable) en vez de asumir un número fijo --
        # ver nota en _VRP_DETAIL_HEADER.
        header_cols = clean_lines[detail_start].strip().split()
        num_fixed_cols = max(len(header_cols) - 1, 1)
        return _parse_vrp_tabular(clean_lines[detail_start + 1:], num_fixed_cols)

    return _parse_vrp_block(clean_lines)


def _parse_vrp_tabular(lines: list[str], num_fixed_cols: int) -> list[VLAN]:
    """Parse the detail table section of 'display vlan' output.

    *num_fixed_cols* -- cantidad de columnas fijas antes de la descripción
    (incluye VID), calculado por el caller a partir del propio header --
    2 anchos reales confirmados, ver nota en ``_VRP_DETAIL_HEADER``."""
    vlans: list[VLAN] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("-"):
            continue
        parts = line.split()
        if not parts[0].isdigit():
            continue
        vlan_id = int(parts[0])
        if vlan_id in _VRP_INTERNAL:
            continue
        description = " ".join(parts[num_fixed_cols:]).strip() if len(parts) > num_fixed_cols else ""
        vlans.append(VLAN(
            vlan_id=vlan_id,
            name=description or f"VLAN{vlan_id:04d}",
        ))
    return vlans


def _parse_vrp_block(lines: list[str]) -> list[VLAN]:
    """Parse block-style 'display vlan' output (older VRP devices)."""
    vlans: list[VLAN] = []
    current_id: int | None = None
    current_name: str | None = None

    for line in lines:
        line = line.strip()
        block_match = _VRP_VLAN_BLOCK.match(line)
        if block_match:
            if current_id is not None and current_id not in _VRP_INTERNAL:
                vlans.append(VLAN(
                    vlan_id=current_id,
                    name=current_name or f"VLAN{current_id:04d}",
                ))
            current_id = int(block_match.group(1))
            current_name = None
            continue
        if current_id is not None:
            name_match = _VRP_NAME_LINE.match(line)
            if name_match:
                current_name = name_match.group(1).strip().rstrip(".")

    if current_id is not None and current_id not in _VRP_INTERNAL:
        vlans.append(VLAN(
            vlan_id=current_id,
            name=current_name or f"VLAN{current_id:04d}",
        ))

    return vlans


def parse_vlan_brief(output: str) -> list[VLAN]:
    """Parse 'show vlan brief' output from Cisco IOS into ``VLAN`` objects.

    Handles real newlines (from events API) and strips any residual ANSI codes.
    Skips IOS-internal VLANs (1, 1002-1005) and non-VLAN lines (headers, ports).

    Parameters
    ----------
    output:
        Raw stdout from the Ansible ``get_vlans`` playbook for an IOS device.

    Returns
    -------
    list[VLAN]
        Normalized VLAN entries, one per user-accessible VLAN.
    """
    vlans: list[VLAN] = []
    for line in output.splitlines():
        line = strip_ansi(line)
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            vlan_id = int(parts[0])
            if vlan_id not in _IOS_INTERNAL:
                vlans.append(VLAN(vlan_id=vlan_id, name=parts[1]))
    return vlans
