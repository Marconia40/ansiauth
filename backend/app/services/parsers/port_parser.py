from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models.port import Puerto, PortMode
from app.services.parsers._common import strip_ansi

logger = logging.getLogger(__name__)


class PortParser(ABC):
    """Shared base for the per-vendor port parsers below — same pattern as
    ``VendorDriver``/``CiscoVendor``/``HuaweiVendor`` (``services/vendors/``):
    what's genuinely identical between platforms (ANSI stripping,
    pseudo-interface filtering) lives here once; each subclass keeps its own
    ``parse_ports()`` plus whatever per-command regexes/column layouts its
    real CLI output needs — those aren't shared because they're not
    actually the same across vendors (confirmed against real devices, not
    assumed).

    ``PSEUDO_PREFIXES``: interface-name prefixes that must never surface as
    a physical switchport (SVIs, loopbacks, tunnels, ...).
    ``PHYSICAL_PREFIXES``: when set, a name must ALSO start with one of
    these to count as physical (Cisco's short interface names are
    ambiguous enough to need an allow-list, not just a deny-list — VRP's
    aren't, so Huawei leaves this ``None``).
    """

    PSEUDO_PREFIXES: tuple[str, ...] = ()
    PHYSICAL_PREFIXES: tuple[str, ...] | None = None

    @classmethod
    def clean(cls, lines: list[str]) -> list[str]:
        """Strip ANSI escapes and trailing whitespace from every line."""
        return [strip_ansi(ln).rstrip() for ln in lines]

    @classmethod
    def is_physical_port(cls, name: str) -> bool:
        """Return True if *name* looks like a real switchport (not an SVI,
        loopback, tunnel, or other pseudo-interface)."""
        if not name or name.startswith(cls.PSEUDO_PREFIXES):
            return False
        if cls.PHYSICAL_PREFIXES is None:
            return True
        return name.startswith(cls.PHYSICAL_PREFIXES)

    @classmethod
    @abstractmethod
    def parse_ports(cls, *raw_outputs: str) -> list[Puerto]:
        """Combine this vendor's read commands into a normalized port
        inventory. Signature (arg count/order) is vendor-specific — see
        each subclass."""
        ...


# ── Huawei VRP ───────────────────────────────────────────────────────────────

class HuaweiPortParser(PortParser):
    # Pseudo-interfaces emitted by VRP that should not surface as switch ports.
    PSEUDO_PREFIXES = (
        "Vlanif",           # SVI
        "NULL",             # Null0
        "LoopBack",         # Loopback
        "Tunnel",
        "Virtual-Template",
        "Eth-Trunk",        # link aggregations are shown separately; not a physical port
        "Cellular",
        "MEth",             # management Ethernet on some chassis platforms
    )

    @classmethod
    def parse_ports(
        cls,
        brief_output: str,
        description_output: str,
        port_vlan_output: str,
        storm_output: str = "",
    ) -> list[Puerto]:
        """Combine four VRP read commands into a normalized port inventory.

        The four inputs are independent — any of them may be empty or missing
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
        storm_output:
            Raw stdout of ``display current-configuration interface``.
            Source of ``storm_control_enabled`` y ``storm_control_threshold``.
            Default empty string por retrocompat con callers viejos que no lo
            pasaban -- el sync real siempre lo pasa.

        Returns
        -------
        list[Puerto]
            One entry per physical switchport, sorted by interface name.
            Pseudo-interfaces (SVIs, loopbacks, NULL0, ...) are filtered out.
        """
        brief_rows = parse_vrp_interface_brief(brief_output) if brief_output else {}
        desc_rows = parse_vrp_interface_description(description_output) if description_output else {}
        vlan_rows = parse_vrp_port_vlan(port_vlan_output) if port_vlan_output else {}
        storm_rows = parse_vrp_storm_control(storm_output) if storm_output else {}

        # Union of port names seen in any of the three sources.
        names = set(brief_rows) | set(desc_rows) | set(vlan_rows)

        ports: list[Puerto] = []
        for name in sorted(names):
            brief = brief_rows.get(name)
            vlan = vlan_rows.get(name)
            description = desc_rows.get(name)
            storm = storm_rows.get(name)

            ports.append(
                Puerto(
                    interface=name,
                    description=description,
                    admin_up=brief.admin_up if brief else None,
                    operational_up=brief.operational_up if brief else None,
                    mode=vlan.mode if vlan else "unknown",
                    access_vlan=vlan.access_vlan if vlan else None,
                    allowed_vlans=vlan.allowed_vlans if vlan else None,
                    storm_control_enabled=storm.enabled if storm else None,
                    storm_control_threshold=storm.threshold if storm else None,
                    # Step 1.1 is intentionally limited to the three commands
                    # above; PoE / speed / duplex are not exposed and remain
                    # None per the "do not invent values" rule.
                    poe_enabled=None,
                    speed=None,
                    duplex=None,
                )
            )

        logger.debug(
            "VRP port parser merged sources: brief=%d desc=%d portvlan=%d storm=%d → ports=%d",
            len(brief_rows), len(desc_rows), len(vlan_rows), len(storm_rows), len(ports),
        )
        return ports


# "display interface brief" y "display port vlan" devuelven el nombre
# completo ("GigabitEthernet0/0/1"), pero "display interface description"
# devuelve la forma abreviada ("GE0/0/1") -- confirmado contra un device
# real (S-series). Bug real encontrado ahí: HuaweiPortParser.parse_ports()
# mergea las 3 fuentes por nombre exacto, así que sin normalizar cada
# puerto físico quedaba duplicado bajo 2 keys distintas (52 puertos reales
# -> 104 filas, cada una con solo parte de los campos poblados, dependiendo
# de qué fuente usó qué spelling).
_VRP_IFACE_ABBREV = (
    ("XGigabitEthernet", "XGE"),
    ("GigabitEthernet", "GE"),
)


def _normalizar_nombre_interfaz(name: str) -> str:
    """Reduce el nombre completo de interfaz VRP a la forma abreviada --
    ver nota en ``_VRP_IFACE_ABBREV``. No-op si *name* ya viene abreviado
    (o es de un tipo sin abreviatura conocida)."""
    for full, short in _VRP_IFACE_ABBREV:
        if name.startswith(full):
            return short + name[len(full):]
    return name


def expandir_nombre_interfaz(name: str) -> str:
    """Inverso de ``_normalizar_nombre_interfaz()`` -- expande la forma
    abreviada ("GE0/0/1") a la forma completa ("GigabitEthernet0/0/1").

    Usado por el driver de escritura (``huawei/driver.py``): confirmado
    contra 2 devices reales que este vendor/platform no tiene un único
    formato de nombre de interfaz para comandos de configuración --
    ``f3r9s2`` (S-series real) rechaza la forma abreviada en ``interface
    {interface}`` ("Error: Wrong parameter found"), mientras que
    ``huawei01`` (CE12800 de lab) rechaza la forma completa con el mismo
    error. El driver manda ambas formas como vars distintas
    (``interface``/``interface_full``) y ``commands.yaml`` reintenta con
    la completa cuando la abreviada falla (mismo mecanismo de
    ``alternatives`` que ya usa el resto de este vendor)."""
    for full, short in _VRP_IFACE_ABBREV:
        if name.startswith(short) and not name.startswith(full):
            return full + name[len(short):]
    return name


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
    ``HuaweiPortParser.is_physical_port``.

    Returns
    -------
    dict[str, _BriefRow]
        Mapping ``interface_name -> _BriefRow``.  Empty if no rows were parsed.
    """
    rows: dict[str, _BriefRow] = {}
    in_table = False
    for line in HuaweiPortParser.clean(output.splitlines()):
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
        name = _normalizar_nombre_interfaz(parts[0])
        if not HuaweiPortParser.is_physical_port(name):
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

    for line in HuaweiPortParser.clean(output.splitlines()):
        if not in_table:
            stripped = line.strip()
            if stripped.startswith("Interface") and "Description" in stripped:
                in_table = True
                # Capture column start positions so we can extract the
                # description column even when it itself contains spaces.
                header_columns = _vrp_column_positions(line, ("Interface", "PHY", "Protocol", "Description"))
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

        name = _normalizar_nombre_interfaz(name)
        if not name or not HuaweiPortParser.is_physical_port(name):
            continue
        descriptions[name] = desc or None
    return descriptions


def _vrp_column_positions(header_line: str, columns: tuple[str, ...]) -> list[tuple[str, int]]:
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

# Matches all three token forms Huawei VRP emits in the Trunk VLAN List column:
#   "X to Y"  — range with keyword (primary VRP format)
#   "X-Y"     — range with hyphen
#   "X"       — single VLAN (also captures the numeric prefix of hybrid-mode
#                entries like "100 tagged" / "1 untagged")
# Using finditer over the whole field handles both space-separated ("50 400")
# and comma-separated ("10, 20, 30 to 32") formats without a prior split.
_VRP_TRUNK_VLAN_TOKEN_RE = re.compile(
    r"(\d+)\s+to\s+(\d+)|(\d+)-(\d+)|(\d+)",
    re.IGNORECASE,
)


def _parse_trunk_vlan_list_vrp(field_value: str) -> list[int] | None:
    """Parse the ``Trunk VLAN List`` column from ``display port vlan``.

    Handles Huawei VRP's space-separated format (``50 400``), range notation
    (``2 to 4094`` or ``2-4094``), comma-separated lists, and hybrid-mode
    qualifiers (``1 untagged, 100 tagged``).

    Returns ``None`` for ``-`` (no trunk VLAN list, e.g. access ports), or
    a sorted, deduplicated list of VLAN IDs otherwise.
    """
    cleaned = field_value.strip()
    if not cleaned or cleaned == "-":
        return None

    vlans: set[int] = set()
    for m in _VRP_TRUNK_VLAN_TOKEN_RE.finditer(cleaned):
        if m.group(1) is not None:
            lo, hi = int(m.group(1)), int(m.group(2))
            if 1 <= lo <= hi <= 4094:
                vlans.update(range(lo, hi + 1))
        elif m.group(3) is not None:
            lo, hi = int(m.group(3)), int(m.group(4))
            if 1 <= lo <= hi <= 4094:
                vlans.update(range(lo, hi + 1))
        elif m.group(5) is not None:
            vid = int(m.group(5))
            if 1 <= vid <= 4094:
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

    for raw_line in HuaweiPortParser.clean(output.splitlines()):
        if not in_table:
            stripped = raw_line.strip()
            if stripped.startswith("Port") and "Link Type" in stripped:
                in_table = True
                header_columns = _vrp_column_positions(
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
                    additional = _parse_trunk_vlan_list_vrp(stripped)
                    if additional is not None:
                        existing = rows[last_name].allowed_vlans or []
                        merged = sorted(set(existing) | set(additional))
                        rows[last_name].allowed_vlans = merged or None
                continue

        name = _normalizar_nombre_interfaz(name)
        if not name or not HuaweiPortParser.is_physical_port(name):
            continue

        mode = _normalize_link_type(link_type)
        try:
            access_vlan: int | None = int(pvid_str) if pvid_str.isdigit() else None
        except ValueError:
            access_vlan = None
        # "desirable"/hybrid (mode == "unknown") reporta "1-4094" en Trunk
        # VLAN List por default aunque el puerto no esté trunkeando nada --
        # mismo bug que Cisco, allowed_vlans solo tiene sentido para un
        # trunk confirmado.
        allowed = _parse_trunk_vlan_list_vrp(trunk_str) if trunk_str and mode == "trunk" else None

        rows[name] = _PortVlanRow(
            name=name,
            mode=mode,
            access_vlan=access_vlan,
            allowed_vlans=allowed,
        )
        last_name = name

    return rows


def parse_vrp_ports(brief_output: str, description_output: str, port_vlan_output: str) -> list[Puerto]:
    """Back-compat free-function wrapper — see ``HuaweiPortParser.parse_ports``."""
    return HuaweiPortParser.parse_ports(brief_output, description_output, port_vlan_output)


# ── Storm control (compartido entre vendors) ────────────────────────────────

@dataclass
class _StormRow:
    """Estado de storm-control por interfaz.

    ``enabled=True`` significa que hay al menos una línea storm-control
    configurada en el device (broadcast o multicast).
    ``threshold`` es percent 0-100 cuando el device lo expresa así;
    None cuando está en pps/bps u otra unidad -- el frontend muestra
    "ON" sin porcentaje en ese caso.
    """
    enabled: bool | None
    threshold: float | None


# Matchea la línea "storm control broadcast ..." o "storm-control broadcast
# ..." en cualquier vendor. Grupo 1 captura el % SI el device lo expresa
# como "percent N" (Huawei) o "level N.NN" (Cisco run-config); grupo 2
# como "N.NN%" (Cisco `show storm-control broadcast`). Al menos uno de los
# dos matchea cuando el device configuró un umbral percent.
_STORM_LEVEL_PERCENT = re.compile(
    r"(?:percent|level)\s+(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*%"
)


def parse_vrp_storm_control(output: str) -> dict[str, _StormRow]:
    """Parse ``display current-configuration interface`` para extraer
    el estado de storm-control por puerto.

    VRP dumpea la config de cada interfaz en secciones delimitadas por
    ``#`` con el header ``interface <name>``. Ausencia de línea ``storm
    control`` en una sección = disabled (False). Presencia = enabled
    (True); si es ``... percent N`` extraemos threshold, si es pps u
    otra forma threshold queda None.

    Multiple flavors observados en producción:
      * ``storm control broadcast min-rate percent 10 max-rate percent 10``
      * ``storm-control broadcast min-rate percent 10 max-rate percent 10``
        (hyphenated, alt syntax)
      * ``storm control broadcast min-rate 100 max-rate 100`` (pps)
    Todas se parsean como enabled=True; solo las percent-form llenan
    threshold.

    Returns
    -------
    dict[str, _StormRow]
        Mapping ``interface_name -> _StormRow``. Contiene TODAS las
        interfaces físicas encontradas en el output, con enabled=False
        para las que no tienen líneas storm-control.
    """
    rows: dict[str, _StormRow] = {}
    current: str | None = None
    for line in HuaweiPortParser.clean(output.splitlines()):
        stripped = line.strip()
        # Header de sección "interface <name>": arranca sección nueva.
        if stripped.startswith("interface "):
            name = stripped.split(None, 1)[1].strip()
            name = _normalizar_nombre_interfaz(name)
            if HuaweiPortParser.is_physical_port(name):
                current = name
                # Sembramos como False; upgrade a True si vemos una
                # línea storm control adentro de la sección.
                rows.setdefault(current, _StormRow(enabled=False, threshold=None))
            else:
                current = None
            continue
        if current is None:
            continue
        # Cierre implícito de sección: separador "#" o comienzo de otro
        # bloque top-level ("aaa", "user-interface", etc.). En VRP los
        # niveles anidados de config van con indentación, así que una
        # línea sin indent que no arranque con "interface" cierra.
        if stripped == "#" or (line and not line.startswith(" ")):
            current = None
            continue
        # Línea de storm control dentro de la sección actual.
        if "storm control" in stripped or "storm-control" in stripped:
            # "storm control action ..." / "... enable trap" no son
            # umbrales; los ignoramos para el threshold pero igual
            # cuentan como enabled=True.
            existing = rows[current]
            threshold = existing.threshold
            match = _STORM_LEVEL_PERCENT.search(stripped)
            if match:
                threshold = float(match.group(1) or match.group(2))
            rows[current] = _StormRow(enabled=True, threshold=threshold)
    return rows


# ── Cisco IOS ────────────────────────────────────────────────────────────────

class CiscoPortParser(PortParser):
    # IOS reports interface names in their short form in every read command
    # (`Gi0/1`, `Te1/0/1`, ...).  These are the prefixes we treat as physical
    # switchports or Layer-2 logical channels.  Anything else (Vlan, Loopback,
    # Tunnel, Null, Management) is filtered out.
    PHYSICAL_PREFIXES = (
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
    PSEUDO_PREFIXES = (
        "Vl",       # Vlan SVI
        "Lo",       # Loopback
        "Tu",       # Tunnel
        "Nu",       # Null
        "Mg",       # Management
        "BV",       # BVI
        "VL",       # case variants
    )

    @classmethod
    def parse_ports(
        cls,
        status_output: str,
        description_output: str,
        switchport_output: str,
        storm_output: str = "",
    ) -> list[Puerto]:
        """Combine four IOS read commands into a normalized port inventory.

        Source-of-truth strategy
        ------------------------
        * **Mode / VLAN information** comes from ``show interfaces switchport``.
          An interface that does not appear there is treated as a routed L3
          port and excluded — exactly mirroring the "ignore non-switch
          interfaces" rule in the spec.
        * **Description / admin state / operational state** come from
          ``show interfaces description``.  When that command does not list the
          port (rare), we fall back to ``show interfaces status``.
        * **Storm-control** comes from ``show storm-control broadcast`` --
          match por interface name; falta = None (no inventar).
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
        storm_output:
            Raw stdout of ``show storm-control broadcast``. Default empty
            string por retrocompat con callers viejos que no lo pasaban --
            el sync real siempre lo pasa.

        Returns
        -------
        list[Puerto]
            Normalized port inventory, sorted by interface name.
        """
        status_rows = parse_ios_interface_status(status_output) if status_output else {}
        desc_rows = parse_ios_interface_description(description_output) if description_output else {}
        sw_rows = parse_ios_switchport(switchport_output) if switchport_output else {}
        storm_rows = parse_ios_storm_control(storm_output) if storm_output else {}

        ports: list[Puerto] = []
        # Source of truth for the port set: switchport_output (real L2 ports).
        for name in sorted(sw_rows.keys()):
            sw = sw_rows[name]
            desc = desc_rows.get(name)
            status = status_rows.get(name)
            storm = storm_rows.get(name)

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
                Puerto(
                    interface=name,
                    description=description,
                    admin_up=admin_up,
                    operational_up=operational_up,
                    mode=sw.mode,
                    access_vlan=sw.access_vlan,
                    allowed_vlans=sw.allowed_vlans,
                    storm_control_enabled=storm.enabled if storm else None,
                    storm_control_threshold=storm.threshold if storm else None,
                    # Step 1.3 explicitly leaves these as None.
                    poe_enabled=None,
                    speed=None,
                    duplex=None,
                )
            )

        logger.debug(
            "IOS port parser merged sources: status=%d desc=%d switchport=%d storm=%d → ports=%d",
            len(status_rows), len(desc_rows), len(sw_rows), len(storm_rows), len(ports),
        )
        return ports


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

    for line in CiscoPortParser.clean(output.splitlines()):
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

        if not CiscoPortParser.is_physical_port(name):
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

    for line in CiscoPortParser.clean(output.splitlines()):
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

        if not CiscoPortParser.is_physical_port(name):
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


_IOS_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
_IOS_NAME_LINE_RE = re.compile(r"^\s*Name\s*:\s*(\S+)")
_IOS_VLAN_WITH_LABEL_RE = re.compile(r"^\s*(\d+)\b")


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
    m = _IOS_VLAN_WITH_LABEL_RE.match(value or "")
    if not m:
        return None
    try:
        vid = int(m.group(1))
    except ValueError:
        return None
    return vid if 1 <= vid <= 4094 else None


def _parse_trunk_vlan_list_ios(value: str) -> list[int] | None:
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
        m = _IOS_RANGE_RE.match(tok)
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
        m2 = _IOS_VLAN_WITH_LABEL_RE.match(tok)
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
        if _IOS_NAME_LINE_RE.match(line):
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
    lines = CiscoPortParser.clean(output.splitlines())
    for block in _split_switchport_blocks(lines):
        name_match = _IOS_NAME_LINE_RE.match(block[0])
        if not name_match:
            continue
        name = name_match.group(1)
        if not CiscoPortParser.is_physical_port(name):
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
            allowed_vlans = _parse_trunk_vlan_list_ios(allowed_raw or "")
        else:
            # "unknown" (dynamic auto/desirable, not actually negotiated to
            # trunk) reports "Trunking VLANs Enabled: ALL" by default even
            # though the port isn't trunking anything -- bug real: eso
            # expandía a la lista completa 1-4094 para cualquier puerto en
            # ese estado. allowed_vlans solo tiene sentido para un trunk
            # confirmado, mismo criterio que "access" ya tenía.
            access_vlan = _parse_vlan_id_with_label(access_vlan_raw or "")
            allowed_vlans = None

        rows[name] = _SwitchportRow(
            name=name,
            switchport_enabled=switchport_enabled,
            mode=mode,
            access_vlan=access_vlan,
            allowed_vlans=allowed_vlans,
        )
    return rows


def parse_ios_ports(status_output: str, description_output: str, switchport_output: str) -> list[Puerto]:
    """Back-compat free-function wrapper — see ``CiscoPortParser.parse_ports``."""
    return CiscoPortParser.parse_ports(status_output, description_output, switchport_output)


# ── show storm-control broadcast (Cisco) ────────────────────────────────────

def parse_ios_storm_control(output: str) -> dict[str, _StormRow]:
    """Parse ``show storm-control broadcast`` output into a per-port row map.

    Layout observado en IOS/IOS-XE::

        Interface  Filter State     Trap State     Upper        Lower        Current
        --------- ---------------  -------------  -----------  -----------  ----------
        Gi1/0/1   Forwarding       inactive        10.00%       10.00%       0.00%
        Gi1/0/2   inactive         inactive        100.00%      100.00%      N/A

    Filter State:
      * ``Forwarding`` / ``Blocking`` -> enabled=True (config aplicada,
        el segundo es "pasó el umbral y está bloqueando ahora")
      * ``inactive`` -> enabled=False (sin config)
      * ``Link Down`` -> enabled=None (no se puede saber sin traer el
        running-config; evitar inventar)

    Upper: umbral configurado. Solo cuando termina en ``%`` extraemos
    threshold; formatos pps/bps quedan como None.
    """
    rows: dict[str, _StormRow] = {}
    in_table = False
    for line in CiscoPortParser.clean(output.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if not in_table:
            if stripped.startswith("Interface") and "Filter" in stripped:
                in_table = True
            continue
        if stripped.startswith("---"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        if not CiscoPortParser.is_physical_port(name):
            continue
        # Filter State puede ser 1 palabra ("Forwarding", "Blocking",
        # "inactive") o 2 ("Link Down"). Detectamos por el token que sigue.
        state_lower = parts[1].lower()
        if state_lower in ("forwarding", "blocking"):
            enabled: bool | None = True
            rest = parts[2:]
        elif state_lower == "inactive":
            enabled = False
            rest = parts[2:]
        elif state_lower == "link" and len(parts) > 2 and parts[2].lower() == "down":
            enabled = None
            rest = parts[3:]
        else:
            # Formato no reconocido -- no inventar valor.
            continue
        # Upper es el primer valor numérico entre los tokens que quedan.
        # Buscamos con la misma regex que usamos en VRP; matchea "10.00%".
        threshold: float | None = None
        for token in rest:
            m = _STORM_LEVEL_PERCENT.search(token)
            if m:
                threshold = float(m.group(1) or m.group(2))
                break
        rows[name] = _StormRow(enabled=enabled, threshold=threshold)
    return rows
