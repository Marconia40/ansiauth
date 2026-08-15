"""Validators for port-management requests.

Validates inputs at the API boundary so cryptic Ansible errors stay out of
client responses.  Validation rules are intentionally permissive — the spec
for Step 2.1 limits writes to descriptions, so we only need to vet
interface names and description strings.
"""

from __future__ import annotations

import re

# Interface names on both Huawei and Cisco use letters, digits, slashes,
# colons, dots, dashes and underscores.  This regex is intentionally a
# whitelist rather than per-vendor format checks: rejecting outright weird
# input is enough at the API boundary; vendor-specific shape (Gi0/0/1 vs
# GigabitEthernet0/0/1) is the device's problem to refuse.
_INTERFACE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_./:\-]{1,63}$")

# Cisco IOS description max is 200 chars; Huawei VRP max is 242 (S-series).
# 200 is the conservative shared ceiling.
_MAX_DESCRIPTION_LEN = 200

# Descriptions can contain spaces and the common ASCII punctuation operators
# actually type into them (and that survive both IOS and VRP).  Newlines and
# control characters are rejected to keep one command = one description.
_DESCRIPTION_INVALID_RE = re.compile(r"[\x00-\x1f\x7f]")


def validate_interface_name(interface: str) -> None:
    """Raise ``ValueError`` if *interface* is not a plausible interface id."""
    if not isinstance(interface, str) or not interface:
        raise ValueError("Interface name is required")
    if not _INTERFACE_NAME_RE.match(interface):
        raise ValueError(
            "Invalid interface name. Allowed characters are letters, digits, "
            "'.', '/', ':', '_', and '-'; name must start with a letter and "
            "be 2–64 characters long."
        )


def validate_access_vlan_id(vlan_id: int) -> None:
    """Validate the VLAN ID for an access-port assignment (Step 2.3).

    Rules:
        * must be a plain int (not a bool — Python treats bool as int)
        * must be in the standard switchport range 1–4094
        * must not be one of the IOS legacy FDDI/Token-Ring VLANs (1002–1005)
          which Cisco refuses to use as an access VLAN

    VLAN 1 is **allowed** because it is the platform default for an access
    port — operators who reset a port to default need to be able to express
    that.  This differs from ``vlan_validator.validate_vlan_not_reserved``
    which is correct for *create/delete* operations on the VLAN itself.
    """
    if isinstance(vlan_id, bool) or not isinstance(vlan_id, int):
        raise ValueError("VLAN ID must be an integer")
    if vlan_id < 1 or vlan_id > 4094:
        raise ValueError(f"VLAN ID {vlan_id} is out of range (1-4094)")
    if vlan_id in (1002, 1003, 1004, 1005):
        raise ValueError(
            f"VLAN {vlan_id} is reserved (Cisco IOS legacy FDDI/Token-Ring) "
            "and cannot be assigned as an access VLAN"
        )


def validate_trunk_vlan_id(vlan_id: int) -> None:
    """Validate a single VLAN ID in a trunk allowed-VLAN list.

    Same range rules as access VLANs, but explicitly documented separately
    because trunk lists have different semantics (VLAN 1 appears commonly as
    a native VLAN that operators legitimately add or remove).
    """
    if isinstance(vlan_id, bool) or not isinstance(vlan_id, int):
        raise ValueError("VLAN ID must be an integer")
    if vlan_id < 1 or vlan_id > 4094:
        raise ValueError(f"VLAN ID {vlan_id} is out of range (1-4094)")
    if vlan_id in (1002, 1003, 1004, 1005):
        raise ValueError(
            f"VLAN {vlan_id} is reserved (Cisco IOS legacy FDDI/Token-Ring) "
            "and cannot appear in a trunk allowed-VLAN list"
        )


def validate_trunk_vlan_list(vlans: list) -> None:
    """Validate a list of VLAN IDs for trunk allowed-VLAN assignment.

    Rules:
        * must be a non-empty list
        * each item must pass ``validate_trunk_vlan_id``
        * duplicates are silently accepted (the execution layer deduplicates)
    """
    if not isinstance(vlans, list):
        raise ValueError("vlans must be a list of integers")
    if len(vlans) == 0:
        raise ValueError("vlans must not be empty")
    for v in vlans:
        validate_trunk_vlan_id(v)


# ── VLAN list compression utilities ──────────────────────────────────────────
# Used by vendor drivers to format the desired VLAN list into the CLI string
# the device expects.  Both functions accept a sorted-or-unsorted list of ints
# and return a compact range string.

def _compress_to_ranges(vlans: list[int]) -> list[tuple[int, int]]:
    """Collapse *vlans* into (start, end) range tuples."""
    if not vlans:
        return []
    sv = sorted(set(vlans))
    ranges: list[tuple[int, int]] = []
    start = sv[0]
    prev = sv[0]
    for v in sv[1:]:
        if v == prev + 1:
            prev = v
        else:
            ranges.append((start, prev))
            start = v
            prev = v
    ranges.append((start, prev))
    return ranges


def compress_vlans_cisco(vlans: list[int]) -> str:
    """Format a VLAN list into the Cisco IOS trunk-allowed syntax.

    Example: [10, 11, 12, 20] → ``"10-12,20"``
    """
    parts = []
    for s, e in _compress_to_ranges(vlans):
        parts.append(f"{s}-{e}" if s != e else str(s))
    return ",".join(parts)


def compress_vlans_huawei(vlans: list[int]) -> str:
    """Format a VLAN list into the Huawei VRP trunk-allowed syntax.

    Example: [10, 11, 12, 20] → ``"10 to 12 20"``
    """
    parts = []
    for s, e in _compress_to_ranges(vlans):
        parts.append(f"{s} to {e}" if s != e else str(s))
    return " ".join(parts)


def validate_description(description: str) -> None:
    """Raise ``ValueError`` if *description* is unsafe to push to a device.

    Empty descriptions are valid — they request a clear.  The driver layer
    interprets the empty string as ``undo description`` / ``no description``.
    """
    if description is None:
        # Treat None as empty (clear).  Callers that need strict typing
        # should enforce that at the Pydantic layer.
        return
    if not isinstance(description, str):
        raise ValueError("Description must be a string")
    if len(description) > _MAX_DESCRIPTION_LEN:
        raise ValueError(
            f"Description must not exceed {_MAX_DESCRIPTION_LEN} characters"
        )
    if _DESCRIPTION_INVALID_RE.search(description):
        raise ValueError("Description must not contain control characters or newlines")
