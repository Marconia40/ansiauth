import logging
import os

import ansible_runner

logger = logging.getLogger(__name__)

# Absolute path to backend/ansible/ regardless of working directory
_ANSIBLE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "ansible")
)


def run_playbook(playbook: str, extravars: dict, inventory: str | None = None) -> dict:
    """Run an Ansible playbook and return rc/stdout/stderr."""
    inv = inventory or os.path.join(_ANSIBLE_DIR, "inventory", "hosts")
    logger.info("Running playbook %s (extravars=%s)", playbook, extravars)

    r = ansible_runner.run(
        private_data_dir=_ANSIBLE_DIR,
        playbook=playbook,
        inventory=inv,
        extravars=extravars,
        quiet=True,
    )

    stdout = ""
    stderr = ""
    try:
        if r.stdout:
            stdout = r.stdout.read() if hasattr(r.stdout, "read") else str(r.stdout)
    except Exception:
        pass
    try:
        if r.stderr:
            stderr = r.stderr.read() if hasattr(r.stderr, "read") else str(r.stderr)
    except Exception:
        pass

    logger.info("Playbook %s finished with rc=%s", playbook, r.rc)
    return {"rc": r.rc, "stdout": stdout, "stderr": stderr}


def build_inventory(device_id: str, ip: str, username: str, password: str) -> str:
    """Build a single-host inventory string from device credentials."""
    return (
        f"{device_id} "
        f"ansible_host={ip} "
        f"ansible_user={username} "
        f"ansible_password={password} "
        f"ansible_network_os=ios "
        f"ansible_connection=network_cli"
    )
