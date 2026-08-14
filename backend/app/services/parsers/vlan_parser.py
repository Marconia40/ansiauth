import re

# IOS-internal VLANs that are not user-managed
_IOS_INTERNAL = {1, 1002, 1003, 1004, 1005}

# VRP default/management VLANs excluded from user-visible output
_VRP_INTERNAL = {1}

_ANSI_ESCAPE = re.compile(r"\x1B\[[0-9;]*m")

# Tabular format: "VID  Type  Status  Property  MAC-LRN  STAT  BC  MC  UC  Description"
_VRP_DETAIL_HEADER = re.compile(r"^VID\s+Type\s+Status", re.IGNORECASE)

# Block format (older VRP style): "The information of VLAN 10:" or "VLAN 10:"
_VRP_VLAN_BLOCK = re.compile(r"(?:The information of )?VLAN\s+(\d+)\s*:", re.IGNORECASE)
_VRP_NAME_LINE = re.compile(r"VLAN\s+Name\s*:\s*(.+)", re.IGNORECASE)


def parse_vrp_vlan_display(output: str) -> list[dict]:
    """Parse 'display vlan' output from Huawei VRP devices into {vlan_id, name} dicts.

    Supports two formats emitted by different VRP versions:
    - Tabular (S-series, common): table with columns VID/Type/Status/.../Description
    - Block (older CE/NE style): "The information of VLAN <id>: / VLAN Name: <name>"

    Skips VLAN 1 (default management VLAN on VRP).
    """
    clean_lines = [_ANSI_ESCAPE.sub("", l) for l in output.splitlines()]

    # Detect tabular format by locating the detail-table header line
    detail_start = next(
        (i for i, l in enumerate(clean_lines) if _VRP_DETAIL_HEADER.match(l.strip())),
        None,
    )
    if detail_start is not None:
        return _parse_vrp_tabular(clean_lines[detail_start + 1:])

    return _parse_vrp_block(clean_lines)


def _parse_vrp_tabular(lines: list[str]) -> list[dict]:
    """Parse the detail table section of 'display vlan' output.

    Expected columns (9 fixed fields before optional description):
      VID  Type  Status  Property  MAC-LRN  STAT  BC  MC  UC  [Description...]
    """
    vlans = []
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
        # columns 1-8 are Type/Status/Property/MAC-LRN/STAT/BC/MC/UC; rest is description
        description = " ".join(parts[9:]).strip() if len(parts) > 9 else ""
        vlans.append({"vlan_id": vlan_id, "name": description or f"VLAN{vlan_id:04d}"})
    return vlans


def _parse_vrp_block(lines: list[str]) -> list[dict]:
    """Parse block-style 'display vlan' output (older VRP devices)."""
    vlans = []
    current_id = None
    current_name = None

    for line in lines:
        line = line.strip()
        block_match = _VRP_VLAN_BLOCK.match(line)
        if block_match:
            if current_id is not None and current_id not in _VRP_INTERNAL:
                vlans.append({"vlan_id": current_id, "name": current_name or f"VLAN{current_id:04d}"})
            current_id = int(block_match.group(1))
            current_name = None
            continue
        if current_id is not None:
            name_match = _VRP_NAME_LINE.match(line)
            if name_match:
                current_name = name_match.group(1).strip().rstrip(".")

    if current_id is not None and current_id not in _VRP_INTERNAL:
        vlans.append({"vlan_id": current_id, "name": current_name or f"VLAN{current_id:04d}"})

    return vlans


def parse_vlan_brief(output: str) -> list[dict]:
    """Parse 'show vlan brief' output into a list of {vlan_id, name} dicts.

    Handles real newlines (from events API) and strips any residual ANSI codes.
    Skips IOS-internal VLANs (1, 1002-1005) and non-VLAN lines (headers, ports).
    """
    vlans = []
    for line in output.splitlines():
        line = _ANSI_ESCAPE.sub("", line)
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            vlan_id = int(parts[0])
            if vlan_id not in _IOS_INTERNAL:
                vlans.append({"vlan_id": vlan_id, "name": parts[1]})
    return vlans
