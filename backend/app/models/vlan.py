from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VLAN:
    """Domain representation of a VLAN — both the read-side data a driver
    returns and the write-side input ``Device`` accepts to create/delete/
    update one. One class, not a DTO/request pair: see
    ``docs/DEVICE_IMPLEMENTATION_PLAN.md`` D7.

    Attributes
    ----------
    vlan_id:
        Numeric VLAN identifier (1–4094, excluding the reserved range).
    name:
        Human-readable label. Required for ``create_vlan``/
        ``update_vlan_description`` (see ``validate_name()``); left empty
        (``""``) when only ``vlan_id`` is needed, e.g. ``delete_vlan``.
    status:
        Optional platform-reported VLAN state (e.g. ``"active"``,
        ``"suspend"``). Populated only by parsers that surface this field;
        ``None`` when the platform does not expose it.

    Validation
    ----------
    ``__post_init__`` always validates ``vlan_id`` (range + not-reserved) —
    safe for both the read path (parsers already exclude reserved VLANs
    before constructing one) and the write path.

    ``name`` format is **not** validated automatically: parsers construct
    ``VLAN`` objects from whatever a real device reports, which can include
    characters (e.g. spaces in a VRP description) that the stricter
    user-input rules reject. Callers that need the name to satisfy those
    rules (``Device.create_vlan``/``update_vlan_description``) call
    ``validate_name()`` explicitly before using it.
    """

    vlan_id: int
    name: str = ""
    status: str | None = None

    def __post_init__(self) -> None:
        from app.validators.vlan_validator import (
            validate_vlan_id_range,
            validate_vlan_not_reserved,
        )
        validate_vlan_id_range(self.vlan_id)
        validate_vlan_not_reserved(self.vlan_id)

    def validate_name(self) -> None:
        """Validate ``name`` against the user-input format rules.

        Not run automatically by ``__post_init__`` — see the class
        docstring. Call this explicitly before using ``name`` in a write
        operation (create / rename).
        """
        from app.validators.vlan_validator import validate_vlan_name
        validate_vlan_name(self.name)

    def to_dict(self) -> dict:
        """Serialize to the ``{"vlan_id": int, "name": str}`` wire format.

        Used at API and JSON-storage boundaries (e.g. job ``pre_state``) to
        preserve full backward compatibility with callers that expect plain
        dicts.  The ``status`` field is intentionally excluded from the wire
        format until consuming layers are updated to handle it.

        Returns
        -------
        dict
            ``{"vlan_id": int, "name": str}``
        """
        return {"vlan_id": self.vlan_id, "name": self.name}

    @classmethod
    def from_dict(cls, data: dict) -> VLAN:
        """Reconstruct a ``VLAN`` from a plain dict.

        Typically used when re-hydrating a record that was previously
        serialized via ``to_dict()`` and stored as JSON (e.g. job
        ``pre_state``).

        Parameters
        ----------
        data:
            Dict with at minimum ``vlan_id`` (int) and ``name`` (str) keys.
            An optional ``status`` key is also read if present.

        Returns
        -------
        VLAN
        """
        return cls(
            vlan_id=data["vlan_id"],
            name=data["name"],
            status=data.get("status"),
        )
