import logging

from app.core.config import EXECUTION_MODE
from app.services import secret_service

logger = logging.getLogger(__name__)

_mock_vlans: list[dict] = [
    {"vlan_id": 10, "name": "MGMT"},
    {"vlan_id": 20, "name": "DATA"},
    {"vlan_id": 30, "name": "VOICE"},
]

_INITIAL_MOCK_VLANS = [
    {"vlan_id": 10, "name": "MGMT"},
    {"vlan_id": 20, "name": "DATA"},
    {"vlan_id": 30, "name": "VOICE"},
]


def reset_mock_vlans() -> None:
    _mock_vlans.clear()
    _mock_vlans.extend(_INITIAL_MOCK_VLANS)


# ── Mock implementations ──────────────────────────────────────────────────────

def _mock_create_vlan(vlan_id: int, device: str) -> dict:
    if device == "fail_device":
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    logger.info("Mock: VLAN %s created on %s", vlan_id, device)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} created", "stderr": ""}


def _mock_delete_vlan(vlan_id: int, device: str) -> dict:
    if device == "fail_device":
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    _mock_vlans[:] = [v for v in _mock_vlans if v["vlan_id"] != vlan_id]
    logger.info("Mock: VLAN %s deleted on %s", vlan_id, device)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} deleted", "stderr": ""}


def _mock_update_vlan(vlan_id: int, description: str, device: str) -> dict:
    if device == "fail_device":
        return {"rc": 1, "stdout": "", "stderr": "Simulated Ansible failure"}
    logger.info("Mock: VLAN %s updated on %s", vlan_id, device)
    return {"rc": 0, "stdout": f"Simulated VLAN {vlan_id} description updated to '{description}'", "stderr": ""}


# ── Device resolution & driver dispatch ──────────────────────────────────────

def _resolve_device(device_id: str):
    from app.services.device_service import get_device
    device = get_device(device_id)
    if not device:
        raise ValueError(f"Device '{device_id}' not found")
    return device


def _get_driver(device):
    from app.services.vendors.dispatcher import get_vendor_driver
    return get_vendor_driver(device.vendor, device.platform)


# ── Public API ────────────────────────────────────────────────────────────────

def create_vlan_on_device(vlan_id: int, name: str, device_id: str) -> dict:
    """Create a VLAN on a single named device. Used for multi-device execution."""
    if EXECUTION_MODE == "mock":
        return _mock_create_vlan(vlan_id, device_id)
    dev = _resolve_device(device_id)
    pw = secret_service.decrypt_password(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info(
        "Real mode: create VLAN %s on %s vendor=%s platform=%s",
        vlan_id, dev.name, dev.vendor, dev.platform,
    )
    return driver.create_vlan(vlan_id, name, dev, pw)


def delete_vlan(vlan_id: int, device_id: str) -> dict:
    if EXECUTION_MODE == "mock":
        return _mock_delete_vlan(vlan_id, device_id)
    dev = _resolve_device(device_id)
    pw = secret_service.decrypt_password(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info(
        "Real mode: delete VLAN %s on %s vendor=%s platform=%s",
        vlan_id, dev.name, dev.vendor, dev.platform,
    )
    return driver.delete_vlan(vlan_id, dev, pw)


def update_vlan_description(vlan_id: int, description: str, device_id: str) -> dict:
    if EXECUTION_MODE == "mock":
        return _mock_update_vlan(vlan_id, description, device_id)
    dev = _resolve_device(device_id)
    pw = secret_service.decrypt_password(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info(
        "Real mode: update VLAN %s on %s vendor=%s platform=%s",
        vlan_id, dev.name, dev.vendor, dev.platform,
    )
    return driver.update_vlan(vlan_id, description, dev, pw)


def get_vlans(device_id: str | None = None) -> list[dict]:
    if EXECUTION_MODE == "mock" or not device_id:
        logger.info("Mock: returning hardcoded VLAN list")
        return _mock_vlans
    dev = _resolve_device(device_id)
    pw = secret_service.decrypt_password(dev.encrypted_password)
    driver = _get_driver(dev)
    logger.info(
        "Real mode: listing VLANs on %s vendor=%s platform=%s",
        dev.name, dev.vendor, dev.platform,
    )
    return driver.get_vlans(dev, pw)


def vlan_exists(device_id: str, vlan_id: int) -> bool:
    """Return True if the given VLAN is already configured on the device."""
    try:
        vlans = get_vlans(device_id)
        return any(v["vlan_id"] == vlan_id for v in vlans)
    except Exception as exc:
        logger.warning(
            "vlan_exists check failed for device=%s vlan=%s: %s — treating as unknown",
            device_id, vlan_id, exc,
        )
        return False
