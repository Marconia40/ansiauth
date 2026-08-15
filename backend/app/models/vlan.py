from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VLANInfo:
    """Normalized domain representation of a single configured VLAN.

    Replaces the raw ``{"vlan_id": int, "name": str}`` dicts that previously
    flowed through the driver → service → API pipeline.  Vendor drivers and
    parsers produce ``VLANInfo`` objects; the API boundary converts them back
    to plain dicts via ``to_dict()`` for HTTP responses and JSON storage.

    Attributes
    ----------
    vlan_id:
        Numeric VLAN identifier (1–4094).
    name:
        Human-readable label assigned to the VLAN.  Parsers always populate
        this field (using an auto-generated ``VLAN<id>`` fallback when the
        device has no explicit name configured).
    status:
        Optional platform-reported VLAN state (e.g. ``"active"``,
        ``"suspend"``).  Populated only by parsers that surface this field;
        ``None`` when the platform does not expose it.
    """

    vlan_id: int
    name: str
    status: str | None = None

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
    def from_dict(cls, data: dict) -> VLANInfo:
        """Reconstruct a ``VLANInfo`` from a plain dict.

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
        VLANInfo
        """
        return cls(
            vlan_id=data["vlan_id"],
            name=data["name"],
            status=data.get("status"),
        )
