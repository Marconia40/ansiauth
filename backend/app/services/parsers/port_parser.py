from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.models.port import PortInfo, PortMode

logger = logging.getLogger(__name__)

# ── Lexical helpers ──────────────────────────────────────────────────────────

# ANSI colour / cursor codes occasionally appear in Huawei terminal output.
_ANSI_ESCAPE = re.compile(r"\x1B\[[0-9;]*m")

# Pseudo-interfaces emitted by VRP that should not surface as switch ports.
_VRP_PSEUDO_PREFIXES = (
    "Vlanif",           # SVI
    "NULL",             # Null0
    "LoopBack",         # Loopback
    "Tunnel",
    "Virtual-Template",
    "Eth-Trunk",        # link aggregations are shown separately; not a physical port
    "Cellular",
    "MEth",             # management Ethernet on some chassis platforms
)


def _is_physical_port(name: str) -> bool:
    """Return True if *name* looks like a physical switchport.

    Filters out SVIs, loopbacks, tunnels and the like which appear in
    ``display interface brief`` output but are not relevant to port-management
    use cases.
    """
    return not name.startswith(_VRP_PSEUDO_PREFIXES)


def _clean(lines: list[str]) -> list[str]:
    """Strip ANSI escapes and trailing whitespace from every line."""
    return [_ANSI_ESCAPE.sub("", ln).rstrip() for ln in lines]


# ── Per-command parsers ──────────────────────────────────────────────────────

@dataclass
class _BriefRow:
    """Internal value object for a row of ``display interface brief``."""
    name: str
    admin_up: bool | None
    operational_up: bool | None


def _parse_admin_state(phy_token: str) -> bool | None:
    """Map the Huawei VRP PHY column token to ``admin_up``.

    VRP marks administratively-disabled ports with a leading ``*`` (rendered
    as ``*down``).  ``^down`` indicates standby and is treated as admin-up
    (the operator did not explicitly shut it down).
    """
    if not phy_token:
        return None
    if phy_token.startswith("*"):
        return False
    # Anything else (up / down / ^down / up(s) / etc.) means the port was
    # not shut down by an operator.  Whether it is *operationally* up is a
    # separate question answered by ``_parse_operational_state``.
    return True


def _parse_operational_state(proto_token: str) -> bool | None:
    """Map the Huawei VRP Protocol column token to ``operational_up``.

    ``up`` and ``up(s)`` (spoofing) are considered operational.  Anything
    else (``down``, ``up(l)`` loopback, etc.) is treated as not operational.
    """
    if not proto_token:
        return None
    head = proto_token.split("(", 1)[0].lower()
    return head == "up"


def parse_vrp_interface_brief(output: str) -> dict[str, _BriefRow]:
    """Parse ``display interface brief`` output into a per-port row map.

    Expected layout (whitespace-aligned columns)::

        Interface                   PHY     Protocol  InUti OutUti   inErrors  outErrors
        GigabitEthernet0/0/1        up      up         0.01%  0.01%        0        0
        GigabitEthernet0/0/2        *down   down       0%     0%           0        0

    Lines preceding the ``Interface`` header are legend/preamble and skipped.
    Pseudo-interfaces (Vlanif, NULL, ...) are filtered out by
    ``_is_physical_port``.

    Returns
    -------
    dict[str, _BriefRow]
        Mapping ``interface_name -> _BriefRow``.  Empty if no rows were parsed.
    """
    rows: dict[str, _BriefRow] = {}
    in_table = False
    for line in _clean(output.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if not in_table:
            # The header line starts with "Interface" and contains "PHY"
            if stripped.startswith("Interface") and "PHY" in stripped:
                in_table = True
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        name = parts[0]
        if not _is_physical_port(name):
            continue
        rows[name] = _BriefRow(
            name=name,
            admin_up=_parse_admin_state(parts[1]),
            operational_up=_parse_operational_state(parts[2]),
        )
    return rows


def parse_vrp_interface_description(output: str) -> dict[str, str | None]:
    """Parse ``display interface description`` output into a name → description map.

    Expected layout::

        Interface                  PHY      Protocol Description
        GigabitEthernet0/0/1       up       up       Workstation01
        GigabitEthernet0/0/2       *down    down     Reserved port
        GigabitEthernet0/0/3       up       up

    Descriptions are everything after the third whitespace-delimited token.
    Empty descriptions are normalized to ``None``.

    Returns
    -------
    dict[str, str | None]
        Mapping ``interface_name -> description``.  Interfaces with no
        configured description are mapped to ``None``.
    """
    descriptions: dict[str, str | None] = {}
    in_table = False
    header_columns: list[tuple[str, int]] = []

    for line in _clean(output.splitlines()):
        if not in_table:
            stripped = line.strip()
            if stripped.startswith("Interface") and "Description" in stripped:
                in_table = True
                # Capture column start positions so we can extract the
                # description column even when it itself contains spaces.
                header_columns = _column_positions(line, ("Interface", "PHY", "Protocol", "Description"))
            continue

        if not line.strip():
            continue

        # Prefer fixed-width column slicing when we successfully captured
        # all four header positions.  Otherwise fall back to a 3-split.
        if len(header_columns) == 4:
            name = line[header_columns[0][1]:header_columns[1][1]].strip()
            desc = line[header_columns[3][1]:].strip()
        else:
            parts = line.strip().split(None, 3)
            if len(parts) < 3:
                continue
            name = parts[0]
            desc = parts[3].strip() if len(parts) == 4 else ""

        if not name or not _is_physical_port(name):
            continue
        descriptions[name] = desc or None
    return descriptions


def _column_positions(header_line: str, columns: tuple[str, ...]) -> list[tuple[str, int]]:
    """Return ``(name, start_offset)`` for each column found in *header_line*.

    Used to drive fixed-width slicing of subsequent rows when columns can
    contain whitespace (e.g. descriptions).  Returns an empty list if any
    column is missing so callers can fall back to split-based parsing.
    """
    positions: list[tuple[str, int]] = []
    cursor = 0
    for col in columns:
        idx = header_line.find(col, cursor)
        if idx == -1:
            return []
        positions.append((col, idx))
        cursor = idx + len(col)
    return positions


# ── Trunk VLAN list parsing ──────────────────────────────────────────────────

_VLAN_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:to|-)\s*(\d+)\s*$", re.IGNORECASE)


def _parse_vlan_token(token: str) -> list[int]:
    """Expand a single token from a Huawei trunk VLAN list to a list of ints.

    Supported token shapes:
        ``"10"``          → [10]
        ``"10 to 12"``    → [10, 11, 12]
        ``"10-12"``       → [10, 11, 12]

    Returns an empty list when the token is empty, ``"-"``, or contains a
    qualifier suffix VRP sometimes appends (e.g. ``"100 untagged"`` —
    parsed digits prefix only).
    """
    cleaned = token.strip()
    if not cleaned or cleaned == "-":
        return []

    range_match = _VLAN_RANGE_RE.match(cleaned)
    if range_match:
        lo, hi = int(range_match.group(1)), int(range_match.group(2))
        if lo <= hi and lo >= 1 and hi <= 4094:
            return list(range(lo, hi + 1))
        return []

    # Hybrid-mode entries look like "100 tagged" / "1 untagged".
    # Take the leading integer and discard the qualifier.
    leading = re.match(r"^\s*(\d+)\b", cleaned)
    if leading:
        return [int(leading.group(1))]
    return []


def _parse_trunk_vlan_list(field_value: str) -> list[int] | None:
    """Parse the ``Trunk VLAN List`` column from ``display port vlan``.

    Returns ``None`` for ``-`` (no trunk VLAN list, e.g. access ports), or
    a sorted, deduplicated list of VLAN IDs otherwise.
    """
    cleaned = field_value.strip()
    if not cleaned or cleaned == "-":
        return None

    vlans: set[int] = set()
    for token in cleaned.split(","):
        for vid in _parse_vlan_token(token):
            vlans.add(vid)
    return sorted(vlans) if vlans else None


def _normalize_link_type(link_type: str) -> PortMode:
    """Map a Huawei link-type token to the normalized ``PortMode``."""
    token = link_type.lower()
    if token == "access":
        return "access"
    if token == "trunk":
        return "trunk"
    # ``hybrid``, ``desirable``, ``negotiation-auto``, etc.
    return "unknown"


@dataclass
class _PortVlanRow:
    name: str
    mode: PortMode
    access_vlan: int | None
    allowed_vlans: list[int] | None


def parse_vrp_port_vlan(output: str) -> dict[str, _PortVlanRow]:
    """Parse ``display port vlan`` output into a per-port row map.

    Expected layout::

        Port                    Link Type    PVID  Trunk VLAN List
        -------------------------------------------------------------------
        GigabitEthernet0/0/1    access       10    -
        GigabitEthernet0/0/2    trunk        1     2 to 4094
        GigabitEthernet0/0/3    hybrid       1     1 untagged, 100 tagged

    Continuation lines (when a trunk VLAN list wraps across multiple rows)
    are merged into the previous interface's allowed-VLAN list.

    Returns
    -------
    dict[str, _PortVlanRow]
        Mapping ``interface_name -> _PortVlanRow``.
    """
    rows: dict[str, _PortVlanRow] = {}
    in_table = False
    header_columns: list[tuple[str, int]] = []
    last_name: str | None = None

    for raw_line in _clean(output.splitlines()):
        if not in_table:
            stripped = raw_line.strip()
            if stripped.startswith("Port") and "Link Type" in stripped:
                in_table = True
                header_columns = _column_positions(
                    raw_line, ("Port", "Link Type", "PVID", "Trunk VLAN List")
                )
            continue

        stripped = raw_line.strip()
        if not stripped or stripped.startswith("-"):
            continue

        # Try fixed-width slicing first; fall back to whitespace split when
        # the header captured fewer than 4 column positions.
        name = link_type = pvid_str = trunk_str = ""
        if len(header_columns) == 4:
            name = raw_line[header_columns[0][1]:header_columns[1][1]].strip()
            link_type = raw_line[header_columns[1][1]:header_columns[2][1]].strip()
            pvid_str = raw_line[header_columns[2][1]:header_columns[3][1]].strip()
            trunk_str = raw_line[header_columns[3][1]:].strip()
        else:
            parts = stripped.split(None, 3)
            if len(parts) >= 4:
                name, link_type, pvid_str, trunk_str = parts[0], parts[1], parts[2], parts[3]
            elif len(parts) == 3:
                name, link_type, pvid_str = parts[0], parts[1], parts[2]
            else:
                # Could be a continuation line — append to previous row's allowed VLANs
                if last_name and last_name in rows:
                    additional = _parse_trunk_vlan_list(stripped)
                    if additional is not None:
                        existing = rows[last_name].allowed_vlans or []
                        merged = sorted(set(existing) | set(additional))
                        rows[last_name].allowed_vlans = merged or None
                continue

        if not name or not _is_physical_port(name):
            continue

        mode = _normalize_link_type(link_type)
        try:
            access_vlan: int | None = int(pvid_str) if pvid_str.isdigit() else None
        except ValueError:
            access_vlan = None
        allowed = _parse_trunk_vlan_list(trunk_str) if trunk_str else None

        rows[name] = _PortVlanRow(
            name=name,
            mode=mode,
            access_vlan=access_vlan,
            allowed_vlans=allowed,
        )
        last_name = name

    return rows


# ── Public entry point ───────────────────────────────────────────────────────

def parse_vrp_ports(
    brief_output: str,
    description_output: str,
    port_vlan_output: str,
) -> list[PortInfo]:
    """Combine three VRP read commands into a normalized port inventory.

    The three inputs are independent — any of them may be empty or missing
    fields without breaking the others.  Per-port fields are merged by
    interface name; gaps are filled with ``None`` (never invented).

    Parameters
    ----------
    brief_output:
        Raw stdout of ``display interface brief``.  Source of ``admin_up``
        and ``operational_up``.
    description_output:
        Raw stdout of ``display interface description``.  Source of
        ``description``.
    port_vlan_output:
        Raw stdout of ``display port vlan``.  Source of ``mode``,
        ``access_vlan`` and ``allowed_vlans``.

    Returns
    -------
    list[PortInfo]
        One entry per physical switchport, sorted by interface name.
        Pseudo-interfaces (SVIs, loopbacks, NULL0, ...) are filtered out.
    """
    brief_rows = parse_vrp_interface_brief(brief_output) if brief_output else {}
    desc_rows = parse_vrp_interface_description(description_output) if description_output else {}
    vlan_rows = parse_vrp_port_vlan(port_vlan_output) if port_vlan_output else {}

    # Union of port names seen in any of the three sources.
    names = set(brief_rows) | set(desc_rows) | set(vlan_rows)

    ports: list[PortInfo] = []
    for name in sorted(names):
        brief = brief_rows.get(name)
        vlan = vlan_rows.get(name)
        description = desc_rows.get(name)

        ports.append(
            PortInfo(
                name=name,
                description=description,
                admin_up=brief.admin_up if brief else None,
                operational_up=brief.operational_up if brief else None,
                mode=vlan.mode if vlan else "unknown",
                access_vlan=vlan.access_vlan if vlan else None,
                allowed_vlans=vlan.allowed_vlans if vlan else None,
                # Step 1.1 is intentionally limited to the three commands
                # above; PoE / speed / duplex are not exposed and remain
                # None per the "do not invent values" rule.
                poe_enabled=None,
                speed=None,
                duplex=None,
            )
        )

    logger.debug(
        "VRP port parser merged sources: brief=%d desc=%d portvlan=%d → ports=%d",
        len(brief_rows), len(desc_rows), len(vlan_rows), len(ports),
    )
    return ports
