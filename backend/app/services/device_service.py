import logging

from app.models.device import Device

logger = logging.getLogger(__name__)

_devices: dict[str, Device] = {
    "mock_device": Device(id="mock_device", ip="192.168.1.1", type="cisco", username="admin", password="admin"),
    "fail_device": Device(id="fail_device", ip="192.168.1.2", type="cisco", username="admin", password="admin"),
    "switch1": Device(id="switch1", ip="10.10.10.10", type="cisco", username="admin", password="cisco123"),
}


def get_device(device_id: str) -> Device | None:
    return _devices.get(device_id)


def get_devices() -> list[Device]:
    return list(_devices.values())


def create_device(device: Device) -> Device:
    _devices[device.id] = device
    logger.info("Device %s registered at %s", device.id, device.ip)
    return device


def delete_device(device_id: str) -> Device | None:
    removed = _devices.pop(device_id, None)
    if removed:
        logger.info("Device %s removed", device_id)
    return removed


def clear_devices() -> None:
    _devices.clear()
    _devices["mock_device"] = Device(id="mock_device", ip="192.168.1.1", type="cisco", username="admin", password="admin")
    _devices["fail_device"] = Device(id="fail_device", ip="192.168.1.2", type="cisco", username="admin", password="admin")
