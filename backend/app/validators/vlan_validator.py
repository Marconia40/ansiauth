def validate_vlan_id_range(vlan_id: int):
    if vlan_id < 1 or vlan_id > 4094:
        raise ValueError(f"VLAN ID {vlan_id} is out of range (1-4094)")


def validate_vlan_not_reserved(vlan_id: int):
    reserved = [1, 1002, 1003, 1004, 1005]
    if vlan_id in reserved:
        raise ValueError(f"VLAN {vlan_id} is reserved")


def validate_vlan_name(name: str):
    if " " in name:
        raise ValueError("VLAN name must not contain spaces")
    if len(name) > 32:
        raise ValueError("VLAN name must not exceed 32 characters")
