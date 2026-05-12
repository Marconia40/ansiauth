import ipaddress
import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator

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
        }
    })

    name: str
    host: str
    vendor: str
    platform: str = "ios"
    username: str
    password: str  # plain text — encrypted before storing


class DeviceUpdate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"host": "192.168.1.20", "username": "netops"}
    })

    host: Optional[str] = None
    vendor: Optional[str] = None
    platform: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

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
    # encrypted_password intentionally excluded from responses
