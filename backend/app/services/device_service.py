import logging

from app.models.device import Device
from app.services import secret_service

logger = logging.getLogger(__name__)


def _make_defaults() -> dict[str, Device]:
    return {
        "mock_device": Device(
            name="mock_device",
            host="192.168.1.1",
            vendor="cisco_ios",
            username="admin",
            encrypted_password=secret_service.encrypt_password("admin"),
        ),
        "fail_device": Device(
            name="fail_device",
            host="192.168.1.2",
            vendor="cisco_ios",
            username="admin",
            encrypted_password=secret_service.encrypt_password("admin"),
        ),
    }


_devices: dict[str, Device] = _make_defaults()


def create_device(name: str, host: str, vendor: str, username: str, password: str) -> Device:
    device = Device(
        name=name,
        host=host,
        vendor=vendor,
        username=username,
        encrypted_password=secret_service.encrypt_password(password),
    )
    _devices[name] = device
    logger.info("Device %s registered at %s", name, host)
    return device


def get_device(name: str) -> Device | None:
    return _devices.get(name)


def get_devices() -> list[Device]:
    return list(_devices.values())


def delete_device(name: str) -> Device | None:
    removed = _devices.pop(name, None)
    if removed:
        logger.info("Device %s removed", name)
    return removed


def clear_devices() -> None:
    _devices.clear()
    _devices.update(_make_defaults())
