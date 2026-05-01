import logging

from app.core.config import EXECUTION_MODE
from app.services import ansible_service, secret_service

logger = logging.getLogger(__name__)

_mock_vlans = [
    {"vlan_id": 10, "name": "MGMT"},
    {"vlan_id": 20, "name": "DATA"},
    {"vlan_id": 30, "name": "VOICE"},
]


# ── Mock implementations ──────────────────────────────────────────────────────

def _mock_create_vlan(vlan_id: int, device: str) -> dict:
    if device == "fail_device":
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    logger.info("Mock: VLAN %s created on %s", vlan_id, device)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} created", "stderr": ""}


def _mock_delete_vlan(vlan_id: int, device: str) -> dict:
    if device == "fail_device":
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    logger.info("Mock: VLAN %s deleted on %s", vlan_id, device)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} deleted", "stderr": ""}


def _mock_update_vlan(vlan_id: int, description: str, device: str) -> dict:
    if device == "fail_device":
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    logger.info("Mock: VLAN %s updated on %s", vlan_id, device)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} description updated to '{description}'", "stderr": ""}


# ── Real (Ansible) implementations ───────────────────────────────────────────

def _resolve_device(device_id: str):
    from app.services.device_service import get_device
    device = get_device(device_id)
    if not device:
        raise ValueError(f"Device '{device_id}' not found")
    return device


def _ansible_create_vlan(vlan_id: int, name: str, device_id: str, device_ip: str, username: str, password: str) -> dict:
    inventory = ansible_service.build_inventory(device_id, device_ip, username, password)
    return ansible_service.run_playbook(
        playbook="create_vlan.yml",
        extravars={"vlan_id": vlan_id, "vlan_name": name, "device": device_id},
        inventory=inventory,
    )


def _ansible_delete_vlan(vlan_id: int, device_id: str, device_ip: str, username: str, password: str) -> dict:
    inventory = ansible_service.build_inventory(device_id, device_ip, username, password)
    return ansible_service.run_playbook(
        playbook="delete_vlan.yml",
        extravars={"vlan_id": vlan_id, "device": device_id},
        inventory=inventory,
    )


def _ansible_update_vlan(vlan_id: int, description: str, device_id: str, device_ip: str, username: str, password: str) -> dict:
    inventory = ansible_service.build_inventory(device_id, device_ip, username, password)
    return ansible_service.run_playbook(
        playbook="update_vlan.yml",
        extravars={"vlan_id": vlan_id, "description": description, "device": device_id},
        inventory=inventory,
    )


def _ansible_get_vlans(device_id: str, device_ip: str, username: str, password: str) -> dict:
    inventory = ansible_service.build_inventory(device_id, device_ip, username, password)
    return ansible_service.run_playbook(
        playbook="get_vlans.yml",
        extravars={"device": device_id},
        inventory=inventory,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def create_vlan(data) -> dict:
    if EXECUTION_MODE == "mock":
        return _mock_create_vlan(data.vlan_id, data.device)
    logger.info("Real mode: create VLAN %s on %s", data.vlan_id, data.device)
    dev = _resolve_device(data.device)
    pw = secret_service.decrypt_password(dev.encrypted_password)
    return _ansible_create_vlan(data.vlan_id, data.name, dev.name, dev.host, dev.username, pw)


def delete_vlan(vlan_id: int, device_id: str) -> dict:
    if EXECUTION_MODE == "mock":
        return _mock_delete_vlan(vlan_id, device_id)
    logger.info("Real mode: delete VLAN %s on %s", vlan_id, device_id)
    dev = _resolve_device(device_id)
    pw = secret_service.decrypt_password(dev.encrypted_password)
    return _ansible_delete_vlan(vlan_id, dev.name, dev.host, dev.username, pw)


def update_vlan_description(vlan_id: int, description: str, device_id: str) -> dict:
    if EXECUTION_MODE == "mock":
        return _mock_update_vlan(vlan_id, description, device_id)
    logger.info("Real mode: update VLAN %s on %s", vlan_id, device_id)
    dev = _resolve_device(device_id)
    pw = secret_service.decrypt_password(dev.encrypted_password)
    return _ansible_update_vlan(vlan_id, description, dev.name, dev.host, dev.username, pw)


def get_vlans(device_id: str | None = None) -> list[dict]:
    if EXECUTION_MODE == "mock" or not device_id:
        logger.info("Mock: returning hardcoded VLAN list")
        return _mock_vlans
    logger.info("Real mode: listing VLANs on %s", device_id)
    dev = _resolve_device(device_id)
    pw = secret_service.decrypt_password(dev.encrypted_password)
    result = _ansible_get_vlans(dev.name, dev.host, dev.username, pw)
    if result["rc"] != 0:
        raise RuntimeError(result["stderr"] or "get_vlans.yml failed")
    from app.services.parsers.vlan_parser import parse_vlan_brief
    return parse_vlan_brief(result["stdout"])
