import re


# Internal VLAN IDs created by IOS that are not real user VLANs
_IOS_INTERNAL = {1002, 1003, 1004, 1005}


def parse_vlan_brief(output: str) -> list[dict]:
    """Parse 'show vlan brief' output into a list of {vlan_id, name} dicts.

    Skips the header lines and IOS-internal VLANs (1002-1005).
    """
    vlans = []
    for line in output.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+active", line)
        if not m:
            continue
        vlan_id = int(m.group(1))
        if vlan_id in _IOS_INTERNAL:
            continue
        vlans.append({"vlan_id": vlan_id, "name": m.group(2)})
    return vlans
