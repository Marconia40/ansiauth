import logging

from app.core.config import EXECUTION_MODE

logger = logging.getLogger(__name__)

_mock_vlans = [
    {"vlan_id": 10, "name": "MGMT"},
    {"vlan_id": 20, "name": "DATA"},
    {"vlan_id": 30, "name": "VOICE"},
]


# ── Mock implementations ──────────────────────────────────────────────────────

def _mock_create_vlan(vlan_id: int, device: str) -> dict:
    if device == "fail_device":
        logger.warning("Mock: simulating failure for device %s", device)
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    if vlan_id == 999:
        logger.warning("Mock: simulating device failure for VLAN 999")
        return {"rc": 1, "stdout": "", "stderr": "Simulated device failure"}
    logger.info("Mock: VLAN %s created on %s", vlan_id, device)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} created", "stderr": ""}


def _mock_delete_vlan(vlan_id: int, device: str) -> dict:
    if device == "fail_device":
        logger.warning("Mock: simulating failure for device %s", device)
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    logger.info("Mock: VLAN %s deleted on %s", vlan_id, device)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} deleted", "stderr": ""}


def _mock_update_vlan(vlan_id: int, description: str, device: str) -> dict:
    if device == "fail_device":
        logger.warning("Mock: simulating failure for device %s", device)
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    logger.info("Mock: VLAN %s updated on %s", vlan_id, device)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} description updated to '{description}'", "stderr": ""}


# ── Real (Ansible) implementations ───────────────────────────────────────────

def _ansible_create_vlan(vlan_id: int, name: str, device_id: str, device_ip: str, username: str, password: str) -> dict:
    from app.services.ansible_runner import build_inventory, run_playbook
    inventory = build_inventory(device_id, device_ip, username, password)
    return run_playbook(
        playbook="create_vlan.yml",
        extravars={"vlan_id": vlan_id, "vlan_name": name, "device": device_id},
        inventory=inventory,
    )


def _ansible_delete_vlan(vlan_id: int, device_id: str, device_ip: str, username: str, password: str) -> dict:
    from app.services.ansible_runner import build_inventory, run_playbook
    inventory = build_inventory(device_id, device_ip, username, password)
    return run_playbook(
        playbook="delete_vlan.yml",
        extravars={"vlan_id": vlan_id, "device": device_id},
        inventory=inventory,
    )


def _ansible_update_vlan(vlan_id: int, description: str, device_id: str, device_ip: str, username: str, password: str) -> dict:
    from app.services.ansible_runner import build_inventory, run_playbook
    inventory = build_inventory(device_id, device_ip, username, password)
    return run_playbook(
        playbook="update_vlan.yml",
        extravars={"vlan_id": vlan_id, "description": description, "device": device_id},
        inventory=inventory,
    )


def _ansible_get_vlans(device_id: str, device_ip: str, username: str, password: str) -> dict:
    from app.services.ansible_runner import build_inventory, run_playbook
    inventory = build_inventory(device_id, device_ip, username, password)
    return run_playbook(
        playbook="get_vlans.yml",
        extravars={"device": device_id},
        inventory=inventory,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def _resolve_device(device_id: str):
    """Return the Device object for a given id (only needed in real mode)."""
    from app.services.device_service import get_device
    device = get_device(device_id)
    if not device:
        raise ValueError(f"Device '{device_id}' not found")
    return device


def create_vlan(data) -> dict:
    if EXECUTION_MODE == "mock":
        return _mock_create_vlan(data.vlan_id, data.device)
    logger.info("Real mode: create VLAN %s on %s", data.vlan_id, data.device)
    dev = _resolve_device(data.device)
    return _ansible_create_vlan(data.vlan_id, data.name, dev.id, dev.ip, dev.username, dev.password)


def delete_vlan(vlan_id: int, device_id: str) -> dict:
    if EXECUTION_MODE == "mock":
        return _mock_delete_vlan(vlan_id, device_id)
    logger.info("Real mode: delete VLAN %s on %s", vlan_id, device_id)
    dev = _resolve_device(device_id)
    return _ansible_delete_vlan(vlan_id, dev.id, dev.ip, dev.username, dev.password)


def update_vlan_description(vlan_id: int, description: str, device_id: str) -> dict:
    if EXECUTION_MODE == "mock":
        return _mock_update_vlan(vlan_id, description, device_id)
    logger.info("Real mode: update VLAN %s on %s", vlan_id, device_id)
    dev = _resolve_device(device_id)
    return _ansible_update_vlan(vlan_id, description, dev.id, dev.ip, dev.username, dev.password)


def get_vlans():
    if EXECUTION_MODE == "mock":
        logger.info("Mock: returning hardcoded VLAN list")
        return _mock_vlans
    logger.info("Real mode: listing VLANs (returns mock list — parse playbook output to extend)")
    return _mock_vlans
