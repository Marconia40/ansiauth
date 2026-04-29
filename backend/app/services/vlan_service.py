import logging
import os
import subprocess

logger = logging.getLogger(__name__)

MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() == "true"


def _mock_create_vlan(vlan_id: int) -> dict:
    if vlan_id == 999:
        logger.warning("Mock mode: simulating device failure for VLAN 999")
        return {"rc": 1, "stdout": "", "stderr": "Simulated device failure"}
    logger.info("Mock mode: simulating VLAN %s creation", vlan_id)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} created", "stderr": ""}


def create_vlan(data):
    if MOCK_MODE:
        return _mock_create_vlan(data.vlan_id)

    logger.info("Real mode: executing Ansible for VLAN %s", data.vlan_id)
    cmd = [
        "ansible-playbook",
        "ansible/playbooks/vlan/create_vlan.yml",
        "-e",
        f"vlan_id={data.vlan_id} vlan_name={data.name}",
        "-l",
        data.device
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "rc": result.returncode
    }
