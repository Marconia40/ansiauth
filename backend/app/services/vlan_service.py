import logging
import os
import subprocess

logger = logging.getLogger(__name__)

MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() == "true"

_mock_vlans = [
    {"vlan_id": 10, "name": "MGMT"},
    {"vlan_id": 20, "name": "DATA"},
    {"vlan_id": 30, "name": "VOICE"},
]


def _mock_create_vlan(vlan_id: int, device: str) -> dict:
    if device == "fail_device":
        logger.warning("Mock mode: simulating Ansible failure for device %s", device)
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    if vlan_id == 999:
        logger.warning("Mock mode: simulating device failure for VLAN 999")
        return {"rc": 1, "stdout": "", "stderr": "Simulated device failure"}
    logger.info("Mock mode: simulating VLAN %s creation", vlan_id)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} created", "stderr": ""}


def _mock_delete_vlan(vlan_id: int, device: str) -> dict:
    if device == "fail_device":
        logger.warning("Mock mode: simulating Ansible failure for device %s", device)
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    logger.info("Mock mode: simulating VLAN %s deletion", vlan_id)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} deleted", "stderr": ""}


def _mock_update_vlan_description(vlan_id: int, description: str, device: str) -> dict:
    if device == "fail_device":
        logger.warning("Mock mode: simulating Ansible failure for device %s", device)
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    logger.info("Mock mode: simulating VLAN %s description update", vlan_id)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} description updated to '{description}'", "stderr": ""}


def create_vlan(data):
    if MOCK_MODE:
        return _mock_create_vlan(data.vlan_id, data.device)

    logger.info("Real mode: executing Ansible for VLAN %s creation", data.vlan_id)
    cmd = [
        "ansible-playbook",
        "ansible/playbooks/vlan/create_vlan.yml",
        "-e",
        f"vlan_id={data.vlan_id} vlan_name={data.name}",
        "-l",
        data.device
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return {"stdout": result.stdout, "stderr": result.stderr, "rc": result.returncode}


def delete_vlan(vlan_id: int, device: str):
    if MOCK_MODE:
        return _mock_delete_vlan(vlan_id, device)

    logger.info("Real mode: executing Ansible for VLAN %s deletion", vlan_id)
    cmd = [
        "ansible-playbook",
        "ansible/playbooks/vlan/delete_vlan.yml",
        "-e",
        f"vlan_id={vlan_id}",
        "-l",
        device
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return {"stdout": result.stdout, "stderr": result.stderr, "rc": result.returncode}


def update_vlan_description(vlan_id: int, description: str, device: str):
    if MOCK_MODE:
        return _mock_update_vlan_description(vlan_id, description, device)

    logger.info("Real mode: executing Ansible for VLAN %s description update", vlan_id)
    cmd = [
        "ansible-playbook",
        "ansible/playbooks/vlan/update_vlan.yml",
        "-e",
        f"vlan_id={vlan_id} description={description}",
        "-l",
        device
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return {"stdout": result.stdout, "stderr": result.stderr, "rc": result.returncode}


def get_vlans():
    if MOCK_MODE:
        logger.info("Mock mode: returning hardcoded VLAN list")
        return _mock_vlans

    logger.info("Real mode: executing Ansible to list VLANs")
    cmd = [
        "ansible-playbook",
        "ansible/playbooks/vlan/get_vlans.yml",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return {"stdout": result.stdout, "stderr": result.stderr, "rc": result.returncode}
