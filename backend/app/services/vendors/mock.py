from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.models.port import Puerto
from app.models.vlan import VLAN
from app.services.vendors.base import VendorDriver

if TYPE_CHECKING:
    from app.models.device import Device

logger = logging.getLogger(__name__)

# Fourth vendor branch, symmetric with vendors/dispatcher.py's real-vendor
# branches (Decision D1b, docs/DEVICE_IMPLEMENTATION_PLAN.md §13). Mirrors
# the mock functions in vlan_service.py / port_service.py; those files keep
# their own independent mock state until Phase 3 deletes them (D3).

_INITIAL_MOCK_VLANS: list[VLAN] = [
    VLAN(vlan_id=10, name="MGMT"),
    VLAN(vlan_id=20, name="DATA"),
    VLAN(vlan_id=30, name="VOICE"),
]

_mock_vlans: list[VLAN] = [
    VLAN(vlan_id=v.vlan_id, name=v.name, status=v.status) for v in _INITIAL_MOCK_VLANS
]


def reset_mock_vlans() -> None:
    """Restore the in-memory mock VLAN list to its initial state (test helper)."""
    _mock_vlans.clear()
    _mock_vlans.extend(
        VLAN(vlan_id=v.vlan_id, name=v.name, status=v.status) for v in _INITIAL_MOCK_VLANS
    )


_INITIAL_MOCK_PORTS: list[Puerto] = [
    Puerto(
        interface="GigabitEthernet0/0/1",
        description="Workstation-01",
        admin_up=True,
        operational_up=True,
        mode="access",
        access_vlan=10,
        allowed_vlans=None,
    ),
    Puerto(
        interface="GigabitEthernet0/0/2",
        description="Workstation-02",
        admin_up=True,
        operational_up=False,
        mode="access",
        access_vlan=20,
        allowed_vlans=None,
    ),
    Puerto(
        interface="GigabitEthernet0/0/24",
        description="Uplink to core",
        admin_up=True,
        operational_up=True,
        mode="trunk",
        access_vlan=1,
        allowed_vlans=[10, 20, 30],
    ),
]


class MockVendor(VendorDriver):
    """Mock driver used when ``EXECUTION_MODE == "mock"`` — VLAN + port
    operations fused into one class (FINAL_ARCHITECTURE.md §1.6;
    ``Device.driver`` is a single property, ver
    `docs/migracion-final-architecture/FASE_1.md` A2).

    VLAN methods mirror vlan_service.py's ``_mock_create_vlan``/
    ``_mock_delete_vlan``/``_mock_update_vlan`` module-level functions. A
    device named ``"fail_device"`` simulates an Ansible failure, matching
    the legacy free-function behavior exactly.

    Port methods mirror port_service.py's ``_mock_list_ports`` and the
    mock-mode branches of its mutation functions. Only the 7 operations
    ``Device`` exposes (§6.1 of docs/DEVICE_IMPLEMENTATION_PLAN.md, plus
    ``set_trunk_pvid_vlan`` — added in Fase 5/A7, ver nota abajo) are
    overridden; the rest keep ``VendorDriver``'s ``NotImplementedError``
    defaults since ``Device`` never calls them.
    """

    def create_vlan(self, vlan_id: int, name: str, device: "Device", password: str) -> dict:
        if device.name == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
        logger.info("Mock: VLAN %s created on %s", vlan_id, device.name)
        return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} created", "stderr": ""}

    def delete_vlan(self, vlan_id: int, device: "Device", password: str) -> dict:
        if device.name == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
        _mock_vlans[:] = [v for v in _mock_vlans if v.vlan_id != vlan_id]
        logger.info("Mock: VLAN %s deleted on %s", vlan_id, device.name)
        return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} deleted", "stderr": ""}

    def update_vlan(self, vlan_id: int, name: str, device: "Device", password: str) -> dict:
        if device.name == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
        logger.info("Mock: VLAN %s updated on %s", vlan_id, device.name)
        return {
            "rc": 0,
            "stdout": f"Simulated VLAN {vlan_id} description updated to '{name}'",
            "stderr": "",
        }

    def save_config(self, device: "Device", password: str) -> dict:
        # No fail_device check here — matches vlan_service.save_config_on_device,
        # which never gates on it in mock mode.
        logger.info("Mock: save config on %s", device.name)
        return {"rc": 0, "stdout": "Simulated config saved", "stderr": ""}

    def get_vlans(self, device: "Device", password: str) -> list[VLAN]:
        logger.info("Mock: returning hardcoded VLAN list")
        return list(_mock_vlans)

    def list_ports(self, device: "Device", password: str) -> list["Puerto"]:
        if device.name == "fail_device":
            raise RuntimeError(f"Simulated port listing failure on device '{device.name}'")
        logger.info(
            "Mock: returning %d ports for device=%s", len(_INITIAL_MOCK_PORTS), device.name
        )
        return [Puerto.from_dict(p.to_dict()) for p in _INITIAL_MOCK_PORTS]

    def update_port_description(
        self, interface: str, description: str, device: "Device", password: str
    ) -> dict:
        if device.name == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: update description on interface=%s device=%s description=%r",
            interface, device.name, description,
        )
        return {"rc": 0, "stdout": "Simulated description updated", "stderr": "", "success": True}

    def set_port_admin_state(
        self, interface: str, enabled: bool, device: "Device", password: str
    ) -> dict:
        if device.name == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: set admin state on interface=%s device=%s enabled=%s",
            interface, device.name, enabled,
        )
        return {"rc": 0, "stdout": "Simulated admin state applied", "stderr": "", "success": True}

    def set_port_access_vlan(
        self, interface: str, vlan_id: int, device: "Device", password: str
    ) -> dict:
        if device.name == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: set access VLAN on interface=%s device=%s vlan_id=%d",
            interface, device.name, vlan_id,
        )
        return {"rc": 0, "stdout": "Simulated access VLAN applied", "stderr": "", "success": True}

    def set_trunk_pvid_vlan(
        self, interface: str, vlan_id: int, device: "Device", password: str
    ) -> dict:
        """Agregado en Fase 5/A7 — corrección real: ``Puerto._aplicar_access_vlan()``
        despacha acá para puertos en modo trunk (GigabitEthernet0/0/24 en
        ``_INITIAL_MOCK_PORTS`` es trunk), pero ``MockVendor`` nunca lo
        implementaba — heredaba el ``NotImplementedError`` default de
        ``VendorDriver`` porque, antes de esta fase, ese camino era
        inalcanzable (docstring de la clase decía "``Device`` never calls
        them", cierto en ese momento). Confirmado con
        ``test_puerto_trunk.py``/``test_api_ports.py``."""
        if device.name == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: set trunk PVID on interface=%s device=%s vlan_id=%d",
            interface, device.name, vlan_id,
        )
        return {"rc": 0, "stdout": "Simulated trunk PVID applied", "stderr": "", "success": True}

    def set_trunk_allowed_vlans(
        self, interface: str, vlan_list: list[int], device: "Device", password: str
    ) -> dict:
        if device.name == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: set trunk VLANs on interface=%s device=%s vlans=%s",
            interface, device.name, vlan_list,
        )
        return {"rc": 0, "stdout": "Simulated trunk VLANs applied", "stderr": "", "success": True}

    def set_access_mode(
        self, interface: str, vlan_id: int, device: "Device", password: str
    ) -> dict:
        if device.name == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: set access mode on interface=%s device=%s vlan_id=%d",
            interface, device.name, vlan_id,
        )
        return {"rc": 0, "stdout": "Simulated access mode applied", "stderr": "", "success": True}

    def set_trunk_mode(
        self, interface: str, native_vlan: int, vlan_list: list[int], device: "Device", password: str
    ) -> dict:
        if device.name == "fail_device":
            return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure", "success": False}
        logger.info(
            "Mock: set trunk mode on interface=%s device=%s native_vlan=%d vlans=%s",
            interface, device.name, native_vlan, vlan_list,
        )
        return {"rc": 0, "stdout": "Simulated trunk mode applied", "stderr": "", "success": True}
