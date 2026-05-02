import re

# IOS-internal VLANs that are not user-managed
_IOS_INTERNAL = {1, 1002, 1003, 1004, 1005}

_ANSI_ESCAPE = re.compile(r"\x1B\[[0-9;]*m")


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
