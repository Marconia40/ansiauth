from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PortMode = Literal["access", "trunk", "unknown"]
PortConfigMode = Literal["access", "trunk"]


@dataclass
class PortInfo:
    """Normalized domain representation of a single switch interface.

    Vendor-neutral by design: every concrete port driver (Cisco, Huawei, ...)
    parses its own CLI output and returns ``PortInfo`` objects.  Callers in
    ``port_service`` and the API layer therefore never see vendor-specific
    fields and can stay fully vendor-agnostic.

    Attributes
    ----------
    name:
        Canonical interface identifier as reported by the device
        (e.g. ``"GigabitEthernet0/0/1"``).
    description:
        Operator-supplied description string, or ``None`` if the device does
        not report one for this interface.  Empty strings are normalized
        to ``None``.
    admin_up:
        Administrative state — ``True`` when the port is *not* shut down.
        Maps to ``"no shutdown"`` on Cisco or absence of ``"shutdown"`` on
        Huawei VRP.  ``None`` only when the platform does not expose this.
    operational_up:
        Line-protocol state — ``True`` when the port is currently passing
        traffic at L1/L2.  Distinct from ``admin_up``: a port can be
        administratively up but operationally down (no link).
    mode:
        Switchport mode: ``"access"``, ``"trunk"``, or ``"unknown"`` when the
        platform reports a value that does not map to either (e.g. ``hybrid``
        on Huawei VRP).
    access_vlan:
        VLAN ID assigned to an access port, or the native VLAN of a trunk
        port (Huawei reports this uniformly as PVID for both modes).
        ``None`` when unknown.
    allowed_vlans:
        For trunk ports, the list of VLAN IDs that the trunk is permitted to
        carry.  ``None`` for access ports or when the platform does not
        expose the trunk VLAN list.  Always sorted ascending.
    poe_enabled:
        Power-over-Ethernet enable state.  ``None`` when the device is not
        PoE-capable or the platform does not report this field.
    speed:
        Negotiated link speed string (e.g. ``"1000Mbps"``), or ``None`` when
        the platform does not expose it in the chosen read commands.
    duplex:
        Negotiated duplex string (e.g. ``"FULL"``, ``"HALF"``), or ``None``.

    Notes
    -----
    The contract is "missing information becomes ``None``".  Parsers must
    never invent values to fill gaps — a missing field is communicated by
    leaving the attribute at its ``None`` default.
    """

    name: str
    description: str | None = None
    admin_up: bool | None = None
    operational_up: bool | None = None
    mode: PortMode = "unknown"
    access_vlan: int | None = None
    allowed_vlans: list[int] | None = None
    poe_enabled: bool | None = None
    speed: str | None = None
    duplex: str | None = None

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for HTTP responses and persistence.

        All keys are always present so consumers can rely on a stable shape;
        unknown values are emitted as ``null``.

        Returns
        -------
        dict
            Wire-format representation with the same field names as the
            domain object.
        """
        return {
            "name": self.name,
            "description": self.description,
            "admin_up": self.admin_up,
            "operational_up": self.operational_up,
            "mode": self.mode,
            "access_vlan": self.access_vlan,
            "allowed_vlans": list(self.allowed_vlans) if self.allowed_vlans is not None else None,
            "poe_enabled": self.poe_enabled,
            "speed": self.speed,
            "duplex": self.duplex,
        }

    @classmethod
    def from_dict(cls, data: dict) -> PortInfo:
        """Reconstruct a ``PortInfo`` from a plain dict.

        Tolerant to missing keys (which default to ``None``) so callers can
        rehydrate partial snapshots — e.g. a record produced by an older
        parser version that lacked PoE awareness.

        Parameters
        ----------
        data:
            Dict shaped like the output of ``to_dict()``.  Only ``name`` is
            strictly required.

        Returns
        -------
        PortInfo
        """
        allowed = data.get("allowed_vlans")
        return cls(
            name=data["name"],
            description=data.get("description"),
            admin_up=data.get("admin_up"),
            operational_up=data.get("operational_up"),
            mode=data.get("mode", "unknown"),
            access_vlan=data.get("access_vlan"),
            allowed_vlans=list(allowed) if allowed is not None else None,
            poe_enabled=data.get("poe_enabled"),
            speed=data.get("speed"),
            duplex=data.get("duplex"),
        )


@dataclass
class PortListResponse:
    """Envelope returned by ``BasePortDriver.list_ports`` and ``port_service.list_ports``.

    Carries the normalized port list plus a small piece of provenance
    metadata so callers can distinguish "device returned zero interfaces" from
    "device returned interfaces we could not parse".  Mirrors the orchestration
    style already used by the VLAN layer where the contract is a plain list,
    but here we wrap the list because port responses tend to be larger and
    benefit from a stable envelope.

    Attributes
    ----------
    device:
        Device name the response refers to.  Allows callers to confirm that
        a fan-out (group execution) returned data for the expected device.
    ports:
        Normalized port entries.  Always present (possibly empty).
    vendor:
        Vendor identifier string the driver belongs to (e.g. ``"huawei_vrp"``).
        Useful for downstream consumers that render vendor-specific hints.
    """

    device: str
    ports: list[PortInfo] = field(default_factory=list)
    vendor: str | None = None

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for HTTP responses."""
        return {
            "device": self.device,
            "vendor": self.vendor,
            "ports": [p.to_dict() for p in self.ports],
            "count": len(self.ports),
        }


@dataclass
class PortConfigRequest:
    """Domain model for a composite port write operation.

    Step 3.1 introduces this model as the architectural foundation for
    multi-field port configuration.  Each field is optional — only the
    supplied fields will be applied.  Validation in ``__post_init__``
    catches cross-field constraint violations before any network I/O.

    Attributes
    ----------
    device:
        Target device name (must be registered in the device inventory).
    interface:
        Vendor-native interface identifier (required; cannot be empty).
    description:
        New description text.  ``""`` clears the description; ``None`` means
        "do not change the description".
    admin_enabled:
        ``True`` → bring the port up; ``False`` → shut it down;
        ``None`` → do not change admin state.
    mode:
        Switchport mode to configure.  ``None`` means "do not change mode".
        Required when ``access_vlan`` or ``allowed_vlans`` is also set,
        because those fields only make sense in a specific mode.
    access_vlan:
        Access VLAN to assign.  Only valid when ``mode='access'``.
    allowed_vlans:
        Trunk allowed-VLAN list.  Only valid when ``mode='trunk'``.

    Validation rules
    ----------------
    * ``interface`` must be a non-empty string.
    * At least one mutation field (description, admin_enabled, mode,
      access_vlan, allowed_vlans) must be non-``None``.
    * ``access_vlan`` is only valid when ``mode='access'``.
    * ``allowed_vlans`` is only valid when ``mode='trunk'``.
    """

    device: str
    interface: str
    description: str | None = None
    admin_enabled: bool | None = None
    mode: PortConfigMode | None = None
    access_vlan: int | None = None
    allowed_vlans: list[int] | None = None

    def __post_init__(self) -> None:
        if not self.interface:
            raise ValueError("'interface' is required and must be a non-empty string")

        _mutation_fields = (
            self.description,
            self.admin_enabled,
            self.mode,
            self.access_vlan,
            self.allowed_vlans,
        )
        if all(v is None for v in _mutation_fields):
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

    @property
    def has_vlan_change(self) -> bool:
        """True if this request includes any VLAN-related field."""
        return self.access_vlan is not None or self.allowed_vlans is not None

    @property
    def mutation_fields(self) -> list[str]:
        """Names of the mutation fields that are non-None in this request."""
        fields = []
        if self.description is not None:
            fields.append("description")
        if self.admin_enabled is not None:
            fields.append("admin_enabled")
        if self.mode is not None:
            fields.append("mode")
        if self.access_vlan is not None:
            fields.append("access_vlan")
        if self.allowed_vlans is not None:
            fields.append("allowed_vlans")
        return fields


@dataclass
class PortConfigResult:
    """Domain model for the result of a composite port write operation.

    Returned by ``BasePortDriver.configure_port`` implementations.  Fields
    marked optional (default ``None``) are populated by the orchestration
    layer or the driver when the information is available.

    Attributes
    ----------
    success:
        ``True`` iff all requested mutations were applied without error.
    changed:
        ``True`` iff at least one field on the device was actually changed
        (i.e. the operation was not a complete no-op).
    interface:
        Vendor-native interface identifier the operation targeted.
    vendor:
        Vendor driver string (e.g. ``"huawei_vrp"``).  ``None`` when the
        driver does not report it.
    execution_time_ms:
        Wall-clock time the driver call consumed, in milliseconds.
    rollback_performed:
        ``True`` if a rollback was triggered after a failure.  ``None``
        when not applicable (success path, or rollback not supported).
    warnings:
        Non-fatal conditions the driver observed (e.g. idempotent no-op
        for a subset of fields).  Empty list is normalized to ``None``.
    """

    success: bool
    changed: bool
    interface: str
    vendor: str | None = None
    execution_time_ms: float | None = None
    rollback_performed: bool | None = None
    warnings: list[str] | None = None

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for HTTP responses."""
        return {
            "success": self.success,
            "changed": self.changed,
            "interface": self.interface,
            "vendor": self.vendor,
            "execution_time_ms": self.execution_time_ms,
            "rollback_performed": self.rollback_performed,
            "warnings": list(self.warnings) if self.warnings is not None else None,
        }
