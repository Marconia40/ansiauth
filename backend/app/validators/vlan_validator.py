import re

_VLAN_NAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')
_VLAN_NAME_ERROR = 'Invalid VLAN name. Only letters, numbers, ".", "_" and "-" are allowed.'


def validate_vlan_id_range(vlan_id: int):
    if vlan_id < 1 or vlan_id > 4094:
        raise ValueError(f"VLAN ID {vlan_id} is out of range (1-4094)")


def validate_vlan_not_reserved(vlan_id: int):
    reserved = [1, 1002, 1003, 1004, 1005]
    if vlan_id in reserved:
        raise ValueError(f"VLAN {vlan_id} is reserved")


def validate_vlan_name(name: str):
    if len(name) > 32:
        raise ValueError("VLAN name must not exceed 32 characters")
    if not _VLAN_NAME_RE.match(name):
        raise ValueError(_VLAN_NAME_ERROR)


def validate_description(description: str):
    if len(description) > 64:
        raise ValueError("Description must not exceed 64 characters")
    if not _VLAN_NAME_RE.match(description):
        raise ValueError(_VLAN_NAME_ERROR)
