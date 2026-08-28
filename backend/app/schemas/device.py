import ipaddress
import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

_HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-\.]*[a-zA-Z0-9])?$")
_IPV4_LIKE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def _validate_host(v: str) -> str:
    try:
        ipaddress.ip_address(v)
        return v
    except ValueError:
        pass
    # Reject strings that look like IPv4 but have out-of-range octets
    if _IPV4_LIKE.match(v):
        raise ValueError(f"'{v}' is not a valid IP address")
    if _HOSTNAME_RE.match(v):
        return v
    raise ValueError(f"'{v}' is not a valid IP address or hostname")


class DeviceCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "name": "switch-01",
            "host": "192.168.1.10",
            "vendor": "cisco",
            "platform": "ios",
            "username": "admin",
            "password": "s3cr3tpass",
            "site_id": 1,
        }
    })

    name: str
    host: str
    vendor: str
    platform: str = "ios"
    username: str
    password: str  # plain text — encrypted before storing
    site_id: Optional[int] = None


class DeviceUpdate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"host": "192.168.1.20", "username": "netops", "site_id": 2}
    })

    host: Optional[str] = None
    vendor: Optional[str] = None
    platform: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    # site_id is also Optional[int], but we distinguish "not provided" from "set to null"
    # via model_dump(exclude_unset=True) in the router — clients pass null to clear.
    site_id: Optional[int] = None

    @field_validator("host")
    @classmethod
    def validate_host(cls, v):
        if v is None:
            return v
        return _validate_host(v)


class DevicePublic(BaseModel):
    id: str
    name: str
    host: str
    vendor: str
    platform: str
    username: str
    site_id: Optional[int] = None
    site_name: Optional[str] = None
    # MSP: Phase 3 — surfaced so UIs can render the owning group without a
    # follow-up call. Populated by ``_to_public`` in ``api/devices.py``.
    device_group_id: Optional[int] = None
    device_group_name: Optional[str] = None
    # encrypted_password intentionally excluded from responses


class DeviceMove(BaseModel):
    """Body for ``POST /devices/{name}/move``.

    Per D8, "remove from group" is a device-level action: passing
    ``device_group_id: null`` moves the device to its current site's Default
    group instead of orphaning it. Passing an integer group id moves the
    device to that group (same-site or cross-site — the dependency picks the
    right ``move_device_*`` op).
    """

    model_config = ConfigDict(json_schema_extra={
        "examples": [
            {"device_group_id": 5},
            {"device_group_id": None},
        ]
    })

    device_group_id: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Target group ID. Pass null to move the device to its current "
            "site's Default group."
        ),
    )
