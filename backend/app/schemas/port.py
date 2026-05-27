from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

PortMode = Literal["access", "trunk", "unknown"]


class PortRead(BaseModel):
    """Wire-format representation of a single port returned by ``GET /ports``.

    Mirrors ``app.models.port.PortInfo`` field-for-field.  Pydantic enforces
    the contract at the API boundary so consumers (frontend, integrations)
    can rely on a fully typed schema in the OpenAPI document.

    All fields are always present in responses; unknown values are emitted
    as ``null`` per the "do not invent values" rule.
    """

    name: str = Field(..., description="Canonical interface identifier as reported by the device.")
    description: Optional[str] = Field(
        None, description="Operator-supplied description, or null if none configured."
    )
    admin_up: Optional[bool] = Field(
        None, description="Administrative state (True = not shut down)."
    )
    operational_up: Optional[bool] = Field(
        None, description="Line-protocol state (True = link up at L1/L2)."
    )
    mode: PortMode = Field(
        "unknown",
        description=(
            "Normalized switchport mode. 'unknown' is used when the platform "
            "reports a value that does not cleanly map to access/trunk "
            "(e.g. Huawei VRP 'hybrid')."
        ),
    )
    access_vlan: Optional[int] = Field(
        None,
        description=(
            "VLAN ID assigned to an access port, or the native VLAN of a "
            "trunk port. Null when unknown."
        ),
    )
    allowed_vlans: Optional[list[int]] = Field(
        None,
        description=(
            "For trunk ports, the list of VLAN IDs permitted on the trunk. "
            "Null for access ports or when the device does not expose this."
        ),
    )
    poe_enabled: Optional[bool] = Field(
        None, description="Power-over-Ethernet enable state, or null when unknown / unsupported."
    )
    speed: Optional[str] = Field(
        None, description="Negotiated link speed string, or null when unknown."
    )
    duplex: Optional[str] = Field(
        None, description="Negotiated duplex string, or null when unknown."
    )


class PortListResponseBody(BaseModel):
    """Envelope returned by ``GET /api/v1/ports/?device=...``.

    Wraps the port list so consumers receive provenance (device + vendor)
    alongside the data, matching the shape produced by
    ``port_service.list_ports``.
    """

    device: str = Field(..., description="Device name the ports belong to.")
    vendor: Optional[str] = Field(
        None,
        description=(
            "Vendor identifier the driver belongs to (e.g. 'huawei_vrp'). "
            "'mock' when the backend is running in EXECUTION_MODE=mock."
        ),
    )
    count: int = Field(..., description="Number of physical ports returned.")
    ports: list[PortRead] = Field(default_factory=list, description="Normalized port entries.")
