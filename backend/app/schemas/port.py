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


class _PortTargetRequest(BaseModel):
    """Shared device+interface target for every single-port write request."""

    device: str = Field(..., min_length=1, description="Target device name.")
    interface: str = Field(
        ...,
        min_length=2,
        max_length=64,
        description="Vendor-native interface name (e.g. 'GigabitEthernet1/0/1').",
    )


class PortDescriptionUpdateRequest(_PortTargetRequest):
    """Request body for ``PATCH /api/v1/ports/description`` (Step 2.1).

    Single device only — the spec deliberately keeps Step 2.1 narrow.
    Empty ``description`` is allowed and means "clear the description".
    """

    description: str = Field(
        default="",
        max_length=200,
        description=(
            "New description text. An empty string clears the description "
            "(`undo description` on Huawei, `no description` on Cisco)."
        ),
    )


class PortAdminStateUpdateRequest(_PortTargetRequest):
    """Request body for ``PATCH /api/v1/ports/admin-state`` (Step 2.2).

    Single device only.  ``enabled=True`` brings the interface up
    (``undo shutdown`` / ``no shutdown``); ``enabled=False`` brings it
    down (``shutdown``).
    """

    enabled: bool = Field(
        ...,
        description="Desired admin state — True to enable, False to disable.",
    )


class PortAccessVlanUpdateRequest(_PortTargetRequest):
    """Request body for ``PATCH /api/v1/ports/access-vlan`` (Step 2.3).

    Single device only.  Sets the access VLAN on an interface that is
    already in access mode.  The orchestration layer validates the current
    mode via pre-state before invoking the driver.
    """

    vlan_id: int = Field(
        ...,
        ge=1,
        le=4094,
        description="Access VLAN ID (1-4094, excluding reserved 1002-1005).",
    )


class PortTrunkVlansUpdateRequest(_PortTargetRequest):
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


class PortSetAccessModeRequest(_PortTargetRequest):
    """Request body for ``POST /api/v1/ports/access-mode``.

    Sets a single interface to access mode with *access_vlan*, atomically.
    Replaces the old generic ``PortConfigureRequest``/``/configure`` for
    this specific, well-defined operation.
    """

    access_vlan: int = Field(
        ...,
        ge=1,
        le=4094,
        description="Access VLAN ID to assign (1-4094).",
    )


class PortSetTrunkModeRequest(_PortTargetRequest):
    """Request body for ``POST /api/v1/ports/trunk-mode``.

    Sets a single interface to trunk mode with *native_vlan* (PVID) and
    *allowed_vlans*, atomically. Both always fully replace whatever the
    port had before — this is a mode change, not an add/remove relative
    to an existing trunk. Use ``PATCH /ports/access-vlan``/
    ``PATCH /ports/trunk-vlans`` to adjust either dimension individually
    on a port that's already trunk.
    """

    native_vlan: int = Field(
        ...,
        ge=1,
        le=4094,
        description="Native VLAN (PVID) for the trunk.",
    )
    allowed_vlans: list[int] = Field(
        ...,
        min_length=1,
        description="Trunk allowed VLANs (non-empty).",
    )


class PortShutdownRequest(_PortTargetRequest):
    """Request body for ``POST /api/v1/ports/shutdown`` (Step 3.3).

    Administratively disables a single interface (``shutdown`` on the device).
    """


class PortEnableRequest(_PortTargetRequest):
    """Request body for ``POST /api/v1/ports/enable`` (Step 3.3).

    Administratively enables a single interface (``no shutdown`` / ``undo shutdown``).
    """


class PortPoeUpdateRequest(_PortTargetRequest):
    """Request body for ``PATCH /api/v1/ports/poe`` (RF-PUERTO-09).

    ``enabled=True`` turns Power-over-Ethernet on (``power inline auto`` /
    ``poe enable``); ``enabled=False`` turns it off (``power inline never``
    / ``poe disable``).
    """

    enabled: bool = Field(
        ...,
        description="Desired PoE state — True to enable, False to disable.",
    )


class PortStormControlUpdateRequest(_PortTargetRequest):
    """Request body for ``PATCH /api/v1/ports/storm-control`` (RF-PUERTO-07).

    Simplified scope (decided with the user): a single enable flag + one
    global percentage threshold, not the 3 traffic types (broadcast/
    multicast/unicast) real hardware exposes separately.
    ``threshold_percent`` is required when ``enabled=True`` — enforced by
    ``Puerto.validar()``, not duplicated here.
    """

    enabled: bool = Field(
        ...,
        description="Desired storm-control state — True to enable, False to disable.",
    )
    threshold_percent: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        description="Broadcast threshold as a percentage of bandwidth (0-100). Required when enabled=True.",
    )


class PortResetRequest(_PortTargetRequest):
    """Request body for ``POST /api/v1/ports/reset`` (RF-PUERTO-10).

    Resets a single interface to its factory-default configuration
    (``default interface`` on Cisco, ``clear configuration interface`` on
    Huawei). Decided with the user: "delete port config" means reset to
    defaults, not a selective per-field undo. No extra fields beyond the
    shared device+interface target.
    """
