from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

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


class PortDescriptionUpdateRequest(BaseModel):
    """Request body for ``PATCH /api/v1/ports/description`` (Step 2.1).

    Single device only — the spec deliberately keeps Step 2.1 narrow.
    Empty ``description`` is allowed and means "clear the description".
    """

    device: str = Field(..., min_length=1, description="Target device name (single device only).")
    interface: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="Vendor-native interface name (e.g. 'GigabitEthernet1/0/1').",
    )
    description: str = Field(
        default="",
        max_length=200,
        description=(
            "New description text. An empty string clears the description "
            "(`undo description` on Huawei, `no description` on Cisco)."
        ),
    )


class PortAdminStateUpdateRequest(BaseModel):
    """Request body for ``PATCH /api/v1/ports/admin-state`` (Step 2.2).

    Single device only.  ``enabled=True`` brings the interface up
    (``undo shutdown`` / ``no shutdown``); ``enabled=False`` brings it
    down (``shutdown``).
    """

    device: str = Field(..., min_length=1, description="Target device name.")
    interface: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="Vendor-native interface name (e.g. 'GigabitEthernet1/0/1').",
    )
    enabled: bool = Field(
        ...,
        description="Desired admin state — True to enable, False to disable.",
    )


class PortAccessVlanUpdateRequest(BaseModel):
    """Request body for ``PATCH /api/v1/ports/access-vlan`` (Step 2.3).

    Single device only.  Sets the access VLAN on an interface that is
    already in access mode.  The orchestration layer validates the current
    mode via pre-state before invoking the driver.
    """

    device: str = Field(..., min_length=1, description="Target device name.")
    interface: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="Vendor-native interface name (e.g. 'GigabitEthernet1/0/1').",
    )
    vlan_id: int = Field(
        ...,
        ge=1,
        le=4094,
        description="Access VLAN ID (1-4094, excluding reserved 1002-1005).",
    )


class PortTrunkVlansUpdateRequest(BaseModel):
    """Request body for ``PATCH /api/v1/ports/trunk-vlans`` (Step 2.4).

    Single device only.  The port must already be in trunk mode.

    ``mode`` controls how the ``vlans`` list is applied relative to the
    current trunk configuration:

    * ``"replace"`` — the trunk is set to exactly *vlans* (clears and resets).
    * ``"add"``     — *vlans* are added to the existing allowed list.
    * ``"remove"``  — *vlans* are removed from the existing allowed list.

    The orchestration layer computes the final desired list and calls the
    vendor driver with the result.  The driver always performs a full replace
    on the device (undo-all + set) so the device state exactly matches
    the computed list.
    """

    device: str = Field(..., min_length=1, description="Target device name.")
    interface: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="Vendor-native interface name (e.g. 'GigabitEthernet1/0/1').",
    )
    mode: Literal["replace", "add", "remove"] = Field(
        ...,
        description=(
            "How vlans is applied to the current trunk config. "
            "'replace' sets the list exactly; 'add' unions with current; "
            "'remove' subtracts from current."
        ),
    )
    vlans: list[int] = Field(
        ...,
        min_length=1,
        description=(
            "VLAN IDs to apply (1-4094, excluding reserved 1002-1005). "
            "Must be a non-empty list. Duplicates are ignored."
        ),
    )


class PortConfigureRequest(BaseModel):
    """Request body for ``PATCH /api/v1/ports/configure`` (Step 3.3).

    Applies one or more port configuration fields in a single driver call.
    At least one mutation field must be non-``None``.

    Cross-field constraints (mirroring ``PortConfigRequest`` domain model):
    * ``access_vlan`` is only valid when ``mode='access'``.
    * ``allowed_vlans`` is only valid when ``mode='trunk'``.
    """

    device: str = Field(..., min_length=1, description="Target device name.")
    interface: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="Vendor-native interface name (e.g. 'GigabitEthernet1/0/1').",
    )
    description: Optional[str] = Field(
        None,
        max_length=200,
        description=(
            "New description. Empty string clears it. "
            "``None`` leaves the description unchanged."
        ),
    )
    admin_enabled: Optional[bool] = Field(
        None,
        description="True to enable, False to disable. None = do not change.",
    )
    mode: Optional[Literal["access", "trunk"]] = Field(
        None,
        description="Switchport mode. None = do not change.",
    )
    access_vlan: Optional[int] = Field(
        None,
        ge=1,
        le=4094,
        description="Access VLAN ID (1-4094). Only valid when mode='access'.",
    )
    allowed_vlans: Optional[list[int]] = Field(
        None,
        min_length=1,
        description="Trunk allowed VLANs. Only valid when mode='trunk'.",
    )
    allowed_vlan_operation: Literal["replace", "add", "remove"] = Field(
        "add",
        description=(
            "How allowed_vlans is applied to the current trunk config when mode='trunk'. "
            "'replace' sets the list exactly (clears existing first); "
            "'add' appends without removing existing; "
            "'remove' removes only the specified VLANs. "
            "Default: 'add'. Ignored when allowed_vlans is None."
        ),
    )

    @model_validator(mode="after")
    def _validate_fields(self) -> "PortConfigureRequest":
        mutation_fields = [
            self.description, self.admin_enabled, self.mode,
            self.access_vlan, self.allowed_vlans,
        ]
        if all(v is None for v in mutation_fields):
            raise ValueError(
                "at least one mutation field must be provided "
                "(description, admin_enabled, mode, access_vlan, or allowed_vlans)"
            )
        if self.access_vlan is not None and self.mode not in ("access", "trunk"):
            raise ValueError(
                f"'access_vlan' (PVID) may only be set when mode='access' or mode='trunk' "
                f"(got mode={self.mode!r})"
            )
        if self.allowed_vlans is not None and self.mode != "trunk":
            raise ValueError(
                f"'allowed_vlans' may only be set when mode='trunk' "
                f"(got mode={self.mode!r})"
            )
        return self


class PortShutdownRequest(BaseModel):
    """Request body for ``POST /api/v1/ports/shutdown`` (Step 3.3).

    Administratively disables a single interface (``shutdown`` on the device).
    """

    device: str = Field(..., min_length=1, description="Target device name.")
    interface: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="Vendor-native interface name (e.g. 'GigabitEthernet1/0/1').",
    )


class PortEnableRequest(BaseModel):
    """Request body for ``POST /api/v1/ports/enable`` (Step 3.3).

    Administratively enables a single interface (``no shutdown`` / ``undo shutdown``).
    """

    device: str = Field(..., min_length=1, description="Target device name.")
    interface: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="Vendor-native interface name (e.g. 'GigabitEthernet1/0/1').",
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
