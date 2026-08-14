from __future__ import annotations


class BaseVendorDriver:
    """Interface that every vendor driver must implement.

    Execution results must always be: {"rc": int, "stdout": str, "stderr": str}
    get_vlans() must always return: list[{"vlan_id": int, "name": str}]
    """

    def create_vlan(self, vlan_id: int, name: str, device, password: str) -> dict:
        raise NotImplementedError

    def delete_vlan(self, vlan_id: int, device, password: str) -> dict:
        raise NotImplementedError

    def update_vlan(self, vlan_id: int, name: str, device, password: str) -> dict:
        raise NotImplementedError

    def get_vlans(self, device, password: str) -> list[dict]:
        raise NotImplementedError

    def save_config(self, device, password: str) -> dict:
        raise NotImplementedError
