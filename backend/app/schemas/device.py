import ipaddress
import re
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
            "device_group_id": 3,
        }
    })

    name: str
    host: str
    vendor: str
    platform: str = "ios"
    username: str
    password: str  # plain text — encrypted before storing
    # MSP: Phase 4 — Site is required. Group is optional; when omitted, the
    # device lands in the Site's Default group. When set, the service
    # rejects (400) if the group's site_id != site_id.
    site_id: int = Field(..., ge=1)
    device_group_id: Optional[int] = Field(default=None, ge=1)


class DeviceUpdate(BaseModel):
    """MSP: Phase 4 — ``site_id`` and ``device_group_id`` are no longer
    accepted here. Callers must use ``POST /devices/{name}/move`` to change a
    device's group (which also changes its site when the target is in a
    different site). Sending either key raises a 422 with a message pointing
    at ``/move``; ``extra='forbid'`` also rejects any other unknown key.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"example": {"host": "192.168.1.20", "username": "netops"}},
    )

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

    @model_validator(mode="before")
    @classmethod
    def _reject_scope_keys(cls, values):
        # Surface a friendlier error than Pydantic's generic "extra_forbidden"
        # for the two keys operators are most likely to try.
        if isinstance(values, dict):
            forbidden = {k for k in ("site_id", "device_group_id") if k in values}
            if forbidden:
                raise ValueError(
                    f"{sorted(forbidden)} cannot be set via PUT /devices/{{name}}; "
                    "use POST /devices/{name}/move to change a device's group or site"
                )
        return values


class DevicePublic(BaseModel):
    id: str
    name: str
    host: str
    vendor: str
    platform: str
    username: str
    # MSP: Phase 4 — site + group are guaranteed to be populated (M3 flipped
    # ``devices.device_group_id`` NOT NULL; site is derived via
    # ``device.device_group.site``).
    site_id: int
    site_name: str
    device_group_id: int
    device_group_name: str
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
