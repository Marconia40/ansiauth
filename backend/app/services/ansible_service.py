import logging

import ansible_runner as _runner

from app.core.config import ANSIBLE_BASE_PATH, INVENTORY_PATH

logger = logging.getLogger(__name__)


def run_playbook(
    playbook: str,
    extravars: dict,
    inventory: str | None = None,
    device: str | None = None,
) -> dict:
    """Execute an Ansible playbook and return rc/stdout/stderr."""
    inv = inventory or INVENTORY_PATH
    device_label = device or extravars.get("device", "unknown")
    logger.info("Running playbook=%s device=%s extravars=%s", playbook, device_label, extravars)
    r = _runner.run(
        private_data_dir=ANSIBLE_BASE_PATH,
        playbook=playbook,
        inventory=inv,
        extravars=extravars,
        quiet=True,
    )
    rc = r.rc
    stdout = _read(r.stdout)
    stderr = _read(r.stderr)
    if rc != 0:
        logger.error("Playbook %s FAILED on device=%s rc=%s stderr=%s", playbook, device_label, rc, stderr)
    else:
        logger.info("Playbook %s SUCCESS on device=%s rc=%s", playbook, device_label, rc)
    return {"rc": rc, "stdout": stdout, "stderr": stderr}


def build_inventory(device_id: str, ip: str, username: str, password: str) -> str:
    """Build a single-host inline inventory string from device credentials."""
    return (
        f"{device_id} "
        f"ansible_host={ip} "
        f"ansible_user={username} "
        f"ansible_password={password} "
        f"ansible_network_os=ios "
        f"ansible_connection=network_cli"
    )


def _read(stream) -> str:
    if not stream:
        return ""
    try:
        return stream.read() if hasattr(stream, "read") else str(stream)
    except Exception:
        return ""
