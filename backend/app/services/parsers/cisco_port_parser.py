from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.models.port import PortInfo, PortMode

logger = logging.getLogger(__name__)

# ── Lexical helpers ──────────────────────────────────────────────────────────

_ANSI_ESCAPE = re.compile(r"\x1B\[[0-9;]*m")

# IOS reports interface names in their short form in every read command
# (`Gi0/1`, `Te1/0/1`, ...).  These are the prefixes we treat as physical
# switchports or Layer-2 logical channels.  Anything else (Vlan, Loopback,
# Tunnel, Null, Management) is filtered out.
_IOS_PHYSICAL_PREFIXES = (
    "Gi",       # GigabitEthernet
    "Fa",       # FastEthernet
    "Te",       # TenGigabitEthernet
    "Tw",       # TwentyFiveGigE / TwoGigabitEthernet
    "Fo",       # FortyGigabitEthernet
    "Hu",       # HundredGigE
    "Et",       # Ethernet (generic / IOS-XE)
    "Po",       # Port-channel  (logical L2 aggregation)
)

# Pseudo / management / routed interfaces that must never surface as ports.
_IOS_PSEUDO_PREFIXES = (
    "Vl",       # Vlan SVI
    "Lo",       # Loopback
    "Tu",       # Tunnel
    "Nu",       # Null
    "Mg",       # Management
    "BV",       # BVI
    "VL",       # case variants
)


def _is_physical_port(name: str) -> bool:
    """Return True when *name* looks like a Cisco L2 switchport.

    Pseudo interfaces (Vlan SVIs, loopbacks, tunnels, ...) are excluded so
    they never bleed into the port inventory.
    """
    if not name:
        return False
    # Pseudo prefixes are explicitly rejected so that an unfortunate
    # ordering of prefix-string matches in physical list never lets one
    # through (e.g. "Vl" wouldn't match any physical prefix anyway, but
    # we belt-and-braces it for clarity).
    if name.startswith(_IOS_PSEUDO_PREFIXES):
        return False
    return name.startswith(_IOS_PHYSICAL_PREFIXES)


def _clean(lines: list[str]) -> list[str]:
    """Strip ANSI escapes and trailing whitespace from every line."""
    return [_ANSI_ESCAPE.sub("", ln).rstrip() for ln in lines]


# ── show interfaces status ───────────────────────────────────────────────────

@dataclass
class _StatusRow:
    """Parsed row of ``show interfaces status``.

    Used as a fallback / cross-check for admin and operational state — the
    description command is the primary source, but ``show interfaces status``
    is what is most reliably present across IOS versions.
    """
    name: str
    status: str          # connected, notconnect, disabled, err-disabled, ...
    vlan_token: str      # "10", "trunk", "routed", "1"
    admin_up: bool | None
    operational_up: bool | None


_STATUS_TO_STATE: dict[str, tuple[bool | None, bool | None]] = {
    # status                  → (admin_up, operational_up)
    "connected":              (True,  True),
    "notconnect":             (True,  False),
    "noconnect":              (True,  False),
    "disabled":               (False, False),
    "err-disabled":           (True,  False),
    "errdisabled":            (True,  False),
    "monitoring":             (True,  True),
    "suspended":              (True,  False),
    "inactive":               (True,  False),
    "down":                   (True,  False),
    "up":                     (True,  True),
}


def parse_ios_interface_status(output: str) -> dict[str, _StatusRow]:
    """Parse ``show interfaces status`` output into a per-port row map.

    Expected layout::

        Port      Name               Status       Vlan       Duplex  Speed Type
        Gi0/1     Workstation01      connected    10         a-full  a-1000 10/100/1000BaseTX
        Gi0/2                        notconnect   20         auto    auto   10/100/1000BaseTX
        Gi0/3     Trunk to core      connected    trunk      full    1000   1000BaseTX SFP
        Gi0/4                        disabled     1          auto    auto   10/100/1000BaseTX

    The ``Name`` column is the description truncated to roughly 19
    characters by IOS.  We do NOT use it as the description source — the
    untruncated value comes from ``show interfaces description``.

    Returns
    -------
    dict[str, _StatusRow]
        ``interface_name → _StatusRow`` for every physical port.  Pseudo
        interfaces are filtered out.
    """
    rows: dict[str, _StatusRow] = {}
    in_table = False
    name_start = status_start = vlan_start = duplex_start = -1

    for line in _clean(output.splitlines()):
        if not in_table:
            stripped = line.strip()
            if stripped.startswith("Port") and "Status" in stripped and "Vlan" in stripped:
                in_table = True
                # Capture column starts to slice fixed-width rows safely —
                # the Name column may contain whitespace.
                name_start = line.find("Name")
                status_start = line.find("Status")
                vlan_start = line.find("Vlan")
                duplex_start = line.find("Duplex")
            continue

        if not line.strip():
            continue
        if not line[:1].isalpha():
            # Continuation / decoration line; skip.
            continue

        if min(name_start, status_start, vlan_start, duplex_start) < 0:
            # We failed to capture all column starts — fall back to a
            # whitespace split which is good enough for well-formed output.
            parts = line.split()
            if len(parts) < 3:
                continue
            name = parts[0]
            status = parts[-4] if len(parts) >= 5 else parts[-3] if len(parts) >= 4 else parts[-2]
            vlan_token = ""
            # try to find the status / vlan tokens heuristically; not used
            # except as a last resort.
            for i, tok in enumerate(parts):
                if tok.lower() in _STATUS_TO_STATE:
                    status = tok
                    vlan_token = parts[i + 1] if i + 1 < len(parts) else ""
                    break
        else:
            name = line[:name_start].strip()
            status = line[status_start:vlan_start].strip()
            vlan_token = line[vlan_start:duplex_start].strip()

        if not _is_physical_port(name):
            continue

        admin_up, oper_up = _STATUS_TO_STATE.get(status.lower(), (None, None))
        rows[name] = _StatusRow(
            name=name,
            status=status,
            vlan_token=vlan_token,
            admin_up=admin_up,
            operational_up=oper_up,
        )
    return rows


# ── show interfaces description ──────────────────────────────────────────────

@dataclass
class _DescriptionRow:
    """Parsed row of ``show interfaces description``."""
    name: str
    admin_up: bool | None
    operational_up: bool | None
    description: str | None


def parse_ios_interface_description(output: str) -> dict[str, _DescriptionRow]:
    """Parse ``show interfaces description`` output.

    Expected layout::

        Interface                      Status         Protocol Description
        Gi0/1                          up             up       Workstation01
        Gi0/2                          admin down     down     Reserved port for printer
        Gi0/3                          up             up       Trunk to core switch
        Gi0/4                          down           down
        Gi0/5                          up (disabled)  down     err-disabled by stp

    Status values mapped to admin_up:
        * ``up``                   → True
        * ``down``                 → True   (link physically down, but operator did not shut it)
        * ``admin down``           → False  (operator-initiated shutdown)
        * ``up (disabled)``        → True   (err-disabled — admin did not shut it)

    Protocol values mapped to operational_up:
        * ``up``   → True
        * ``down`` → False

    Returns
    -------
    dict[str, _DescriptionRow]
    """
    rows: dict[str, _DescriptionRow] = {}
    in_table = False
    iface_start = status_start = protocol_start = description_start = -1

    for line in _clean(output.splitlines()):
        if not in_table:
            stripped = line.strip()
            if (
                stripped.startswith("Interface")
                and "Status" in stripped
                and "Protocol" in stripped
                and "Description" in stripped
            ):
                in_table = True
                iface_start = line.find("Interface")
                status_start = line.find("Status")
                protocol_start = line.find("Protocol")
                description_start = line.find("Description")
            continue

        if not line.strip():
            continue
        if not line[:1].isalpha():
            continue

        # Prefer fixed-width slicing — the description column can contain
        # spaces and "Status" can be "admin down" (two words).
        if (
            iface_start >= 0
            and status_start > iface_start
            and protocol_start > status_start
            and description_start > protocol_start
        ):
            name = line[iface_start:status_start].strip()
            status_tok = line[status_start:protocol_start].strip()
            proto_tok = line[protocol_start:description_start].strip()
            desc = line[description_start:].rstrip()
        else:
            # Fallback split — best-effort, accept some inaccuracy on the
            # description column for unusual output formats.
            parts = line.split(None, 4)
            if len(parts) < 3:
                continue
            name = parts[0]
            # Status can be "admin down" (two tokens) — detect by looking
            # at the third token and merging if it's "down"/"up" plus the
            # second is "admin" or "up".
            if len(parts) >= 4 and parts[1].lower() == "admin" and parts[2].lower() == "down":
                status_tok = "admin down"
                proto_tok = parts[3] if len(parts) > 3 else ""
                desc = parts[4].strip() if len(parts) > 4 else ""
            else:
                status_tok = parts[1]
                proto_tok = parts[2] if len(parts) > 2 else ""
                desc = " ".join(parts[3:]).strip() if len(parts) > 3 else ""

        if not _is_physical_port(name):
            continue

        admin_up = _parse_description_admin(status_tok)
        oper_up = _parse_description_protocol(proto_tok)

        rows[name] = _DescriptionRow(
            name=name,
            admin_up=admin_up,
            operational_up=oper_up,
            description=desc.strip() or None,
        )
    return rows


def _parse_description_admin(status_token: str) -> bool | None:
    """Map the ``Status`` token from ``show interfaces description`` to admin_up."""
    if not status_token:
        return None
    token = status_token.strip().lower()
    if token.startswith("admin"):
        # "admin down" / "administratively down"
        return False
    return True


def _parse_description_protocol(protocol_token: str) -> bool | None:
    """Map the ``Protocol`` token from ``show interfaces description`` to operational_up."""
    if not protocol_token:
        return None
    head = protocol_token.strip().lower().split()[0]
    return head == "up"


# ── show interfaces switchport ───────────────────────────────────────────────

@dataclass
class _SwitchportRow:
    """Parsed block from ``show interfaces switchport``."""
    name: str
    switchport_enabled: bool
    mode: PortMode
    access_vlan: int | None
    allowed_vlans: list[int] | None


_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
_NAME_LINE_RE = re.compile(r"^\s*Name\s*:\s*(\S+)")
_VLAN_WITH_LABEL_RE = re.compile(r"^\s*(\d+)\b")


def _normalize_mode(mode_token: str) -> PortMode:
    """Map a Cisco IOS Operational Mode token to the normalized ``PortMode``."""
    token = mode_token.strip().lower()
    if not token:
        return "unknown"
    if "trunk" in token:
        return "trunk"
    if "access" in token:
        return "access"
    # dynamic auto / dynamic desirable / down / etc.
    return "unknown"


def _parse_vlan_id_with_label(value: str) -> int | None:
    """Extract a numeric VLAN ID from values like ``"10 (HQ_VLAN_10)"`` or ``"1 (default)"``."""
    m = _VLAN_WITH_LABEL_RE.match(value or "")
    if not m:
        return None
    try:
        vid = int(m.group(1))
    except ValueError:
        return None
    return vid if 1 <= vid <= 4094 else None


def _parse_trunk_vlan_list(value: str) -> list[int] | None:
    """Parse a Cisco trunk VLAN list specification into a sorted ID list.

    Handles:
        * ``"ALL"``        → ``list(range(1, 4095))``
        * ``"NONE"``       → ``[]``
        * ``"1,3-10,20"``  → ``[1, 3, 4, ..., 10, 20]``
        * ``"1-4094"``     → ``[1, 2, ..., 4094]``
        * Multi-line continuation strings already concatenated by the caller.

    Returns
    -------
    list[int] | None
        Sorted unique VLAN IDs, or ``None`` when the value cannot be parsed.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    upper = cleaned.upper()
    if upper == "ALL":
        return list(range(1, 4095))
    if upper == "NONE":
        return []

    vlans: set[int] = set()
    for token in cleaned.split(","):
        tok = token.strip()
        if not tok:
            continue
        m = _RANGE_RE.match(tok)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if 1 <= lo <= hi <= 4094:
                vlans.update(range(lo, hi + 1))
            continue
        if tok.isdigit():
            vid = int(tok)
            if 1 <= vid <= 4094:
                vlans.add(vid)
            continue
        # Some IOS variants append qualifiers like "10 (active)" — take the
        # leading integer.
        m2 = _VLAN_WITH_LABEL_RE.match(tok)
        if m2:
            try:
                vid = int(m2.group(1))
            except ValueError:
                continue
            if 1 <= vid <= 4094:
                vlans.add(vid)
    return sorted(vlans) if vlans else None


def _split_switchport_blocks(lines: list[str]) -> list[list[str]]:
    """Split ``show interfaces switchport`` output into per-interface blocks.

    Each block starts at a ``Name: <iface>`` line and ends at the next
    ``Name:`` line or end-of-input.  Lines before the first ``Name:`` are
    discarded (preamble).
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    started = False
    for line in lines:
        if _NAME_LINE_RE.match(line):
            if started:
                blocks.append(current)
            current = [line]
            started = True
        elif started:
            current.append(line)
    if started:
        blocks.append(current)
    return blocks


def _extract_field(block: list[str], key: str) -> str | None:
    """Return the value of ``Key: value`` in *block*, or None if missing.

    Handles continuation lines: if the value wraps, subsequent indented or
    bare numeric lines (before the next ``Key:`` pattern) are appended.
    """
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.*)$")
    other_key = re.compile(r"^\s*[A-Z][\w\-/ ]*\s*:\s")
    value_parts: list[str] = []
    started = False

    for line in block:
        if not started:
            m = pattern.match(line)
            if m:
                value_parts.append(m.group(1).strip())
                started = True
            continue

        if other_key.match(line):
            break
        stripped = line.strip()
        if not stripped:
            break
        value_parts.append(stripped)

    if not value_parts:
        return None
    # Join with comma so wrapped trunk lists like
    #   "1-100,200-300,"
    #   "400,500,600"
    # are reconstituted into "1-100,200-300,400,500,600" without breaking
    # individual numeric ranges.
    joined = ",".join(p.rstrip(",") for p in value_parts)
    return joined.strip() or None


def parse_ios_switchport(output: str) -> dict[str, _SwitchportRow]:
    """Parse ``show interfaces switchport`` into a per-port mode/VLAN map.

    Honours the ``Switchport: Enabled`` flag — when switchport mode is
    disabled the interface is treated as a routed L3 port and excluded.

    Returns
    -------
    dict[str, _SwitchportRow]
    """
    rows: dict[str, _SwitchportRow] = {}
    lines = _clean(output.splitlines())
    for block in _split_switchport_blocks(lines):
        name_match = _NAME_LINE_RE.match(block[0])
        if not name_match:
            continue
        name = name_match.group(1)
        if not _is_physical_port(name):
            continue

        sw_enabled_raw = _extract_field(block, "Switchport")
        switchport_enabled = (sw_enabled_raw or "").lower().startswith("enab")
        if not switchport_enabled:
            # Routed / L3 interface — out of scope for port-management.
            continue

        # IOS reports both "Administrative Mode" and "Operational Mode".  We
        # normalize the operational mode because it reflects what the port
        # is actually doing.  When operational mode is "down" (port not
        # negotiating), we fall back to the administrative intent.
        op_mode = _extract_field(block, "Operational Mode") or ""
        admin_mode = _extract_field(block, "Administrative Mode") or ""
        mode = _normalize_mode(op_mode)
        if mode == "unknown":
            mode = _normalize_mode(admin_mode)

        access_vlan_raw = _extract_field(block, "Access Mode VLAN")
        native_vlan_raw = _extract_field(block, "Trunking Native Mode VLAN")
        allowed_raw = _extract_field(block, "Trunking VLANs Enabled")

        # access_vlan policy mirrors the Huawei parser:
        #   * access port → VLAN from "Access Mode VLAN"
        #   * trunk  port → native VLAN from "Trunking Native Mode VLAN"
        if mode == "trunk":
            access_vlan = _parse_vlan_id_with_label(native_vlan_raw or "")
            allowed_vlans = _parse_trunk_vlan_list(allowed_raw or "")
        elif mode == "access":
            access_vlan = _parse_vlan_id_with_label(access_vlan_raw or "")
            allowed_vlans = None
        else:
            access_vlan = _parse_vlan_id_with_label(access_vlan_raw or "")
            allowed_vlans = _parse_trunk_vlan_list(allowed_raw or "") if allowed_raw else None

        rows[name] = _SwitchportRow(
            name=name,
            switchport_enabled=switchport_enabled,
            mode=mode,
            access_vlan=access_vlan,
            allowed_vlans=allowed_vlans,
        )
    return rows


# ── Public entry point ───────────────────────────────────────────────────────

def parse_ios_ports(
    status_output: str,
    description_output: str,
    switchport_output: str,
) -> list[PortInfo]:
    """Combine three IOS read commands into a normalized port inventory.

    Source-of-truth strategy
    ------------------------
    * **Mode / VLAN information** comes from ``show interfaces switchport``.
      An interface that does not appear there is treated as a routed L3
      port and excluded — exactly mirroring the "ignore non-switch
      interfaces" rule in the spec.
    * **Description / admin state / operational state** come from
      ``show interfaces description``.  When that command does not list the
      port (rare), we fall back to ``show interfaces status``.
    * **PoE / speed / duplex** are deliberately left as ``None`` per the
      step 1.3 spec.  ``show interfaces status`` does expose speed and
      duplex but populating them is reserved for a future step.

    Parameters
    ----------
    status_output:
        Raw stdout of ``show interfaces status``.
    description_output:
        Raw stdout of ``show interfaces description``.
    switchport_output:
        Raw stdout of ``show interfaces switchport``.

    Returns
    -------
    list[PortInfo]
        Normalized port inventory, sorted by interface name.
    """
    status_rows = parse_ios_interface_status(status_output) if status_output else {}
    desc_rows = parse_ios_interface_description(description_output) if description_output else {}
    sw_rows = parse_ios_switchport(switchport_output) if switchport_output else {}

    ports: list[PortInfo] = []
    # Source of truth for the port set: switchport_output (real L2 ports).
    for name in sorted(sw_rows.keys()):
        sw = sw_rows[name]
        desc = desc_rows.get(name)
        status = status_rows.get(name)

        if desc is not None:
            admin_up = desc.admin_up
            operational_up = desc.operational_up
            description = desc.description
        elif status is not None:
            admin_up = status.admin_up
            operational_up = status.operational_up
            description = None
        else:
            admin_up = None
            operational_up = None
            description = None

        ports.append(
            PortInfo(
                name=name,
                description=description,
                admin_up=admin_up,
                operational_up=operational_up,
                mode=sw.mode,
                access_vlan=sw.access_vlan,
                allowed_vlans=sw.allowed_vlans,
                # Step 1.3 explicitly leaves these as None.
                poe_enabled=None,
                speed=None,
                duplex=None,
            )
        )

    logger.debug(
        "IOS port parser merged sources: status=%d desc=%d switchport=%d → ports=%d",
        len(status_rows), len(desc_rows), len(sw_rows), len(ports),
    )
    return ports
