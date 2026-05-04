import logging
import os
import tempfile

import ansible_runner as _runner

from app.core.config import ANSIBLE_BASE_PATH, INVENTORY_PATH

logger = logging.getLogger(__name__)


def validate_inventory(inventory: str) -> None:
    """Raise ValueError if inventory string is missing required fields."""
    parts = inventory.split()
    if not parts:
        raise ValueError("Inventory string is empty")
    if not parts[0]:
        raise ValueError("Inventory hostname is missing")
    if not any(p.startswith("ansible_host=") for p in parts):
        raise ValueError("Inventory is missing ansible_host")


def run_playbook(
    playbook: str,
    extravars: dict,
    inventory: str | None = None,
    device: str | None = None,
) -> dict:
    """Execute an Ansible playbook and return rc/stdout/stderr."""
    if inventory is None:
        logger.warning("No dynamic inventory provided for playbook=%s, falling back to %s", playbook, INVENTORY_PATH)
    inv = inventory or INVENTORY_PATH
    device_label = device or extravars.get("device", "unknown")
    logger.info("Running playbook=%s device=%s", playbook, device_label)
    logger.info("Extravars: %s", extravars)
    logger.info("Inventory:\n%s", inv)

    # ansible-runner's dump_artifacts() writes inline inventory strings to
    # private_data_dir/inventory/hosts. When concurrent calls share the same
    # private_data_dir (ANSIBLE_BASE_PATH), each call overwrites the previous
    # one's inventory file, causing both subprocesses to target the same device.
    # Writing the inventory to a unique temp file makes dump_artifacts treat it
    # as an absolute path and skip the shared-file write entirely.
    _temp_inv = None
    if isinstance(inv, str) and not os.path.exists(inv):
        _temp_inv = tempfile.NamedTemporaryFile(mode="w", suffix=".ini", delete=False)
        _temp_inv.write(inv)
        _temp_inv.close()
        inv = _temp_inv.name

    try:
        r = _runner.run(
            private_data_dir=ANSIBLE_BASE_PATH,
            playbook=playbook,
            inventory=inv,
            extravars=extravars,
            quiet=True,
        )
    finally:
        if _temp_inv is not None:
            os.unlink(_temp_inv.name)

    rc = r.rc
    stderr = _read(r.stderr)
    # ios_command stores output in events (res.stdout list), not in the text stdout file.
    # Fall back to text stdout for playbooks that don't produce structured events (ios_config etc).
    stdout = _extract_ios_command_output(r) or _read(r.stdout)
    combined_output = (stdout + stderr).lower()
    if "no hosts matched" in combined_output and rc == 0:
        logger.error("Playbook %s: no hosts matched on device=%s — treating as failure", playbook, device_label)
        rc = 1
        stderr = stderr or "No hosts matched in inventory"
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


def _extract_ios_command_output(r) -> str:
    """Return the first ios_command stdout string from ansible-runner events.

    ios_command stores each command's output in event_data.res.stdout (a list).
    This is the only reliable way to get the raw device output — the text stdout
    file embeds it as a JSON-escaped single line inside the debug task output.
    """
    try:
        for event in r.events:
            if event.get("event") == "runner_on_ok":
                res = event.get("event_data", {}).get("res", {})
                stdout_list = res.get("stdout")
                if isinstance(stdout_list, list) and stdout_list:
                    logger.debug("Extracted ios_command output from events (%d chars)", len(stdout_list[0]))
                    return stdout_list[0]
    except Exception as exc:
        logger.debug("Could not extract command output from events: %s", exc)
    return ""


def _read(stream) -> str:
    if not stream:
        return ""
    try:
        return stream.read() if hasattr(stream, "read") else str(stream)
    except Exception:
        return ""
