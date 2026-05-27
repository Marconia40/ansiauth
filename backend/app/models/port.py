from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

PortMode = Literal["access", "trunk", "unknown"]


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
