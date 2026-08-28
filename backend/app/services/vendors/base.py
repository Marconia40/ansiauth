from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.vlan import VLAN


class BaseVendorDriver(ABC):
    """Abstract base class that every vendor VLAN driver must implement.

    All concrete drivers must satisfy this interface so that callers in
    vlan_service and vlan_execution_service can remain fully vendor-agnostic.

    Return-value contracts
    ----------------------
    Mutation operations (create / update / delete / save_config):
        dict with keys:
            rc      – int  : Ansible return code (0 = success, non-zero = failure)
            stdout  – str  : combined playbook stdout
            stderr  – str  : combined playbook stderr
            success – bool : True iff rc == 0  (normalized convenience field)

    Query operations (list_vlans / get_vlans / get_vlan):
        list_vlans / get_vlans → list[VLAN]
        get_vlan               → VLAN | None
    """

    # ── Mutation operations (must be implemented by every driver) ────────────

    @abstractmethod
    def create_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        """Provision a new VLAN on the target device.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier (1–4094).
        name:
            Human-readable label to assign to the VLAN.
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    @abstractmethod
    def delete_vlan(self, vlan_id: int, device: Device, password: str) -> dict:
        """Remove an existing VLAN from the target device.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier of the VLAN to remove.
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    @abstractmethod
    def update_vlan(self, vlan_id: int, name: str, device: Device, password: str) -> dict:
        """Rename / update the description of an existing VLAN.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier of the VLAN to update.
        name:
            New label to assign to the VLAN.
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    @abstractmethod
    def save_config(self, device: Device, password: str) -> dict:
        """Persist the running configuration to non-volatile storage.

        On platforms where changes are committed automatically (e.g. some
        Cisco IOS versions), implementations should raise ``NotImplementedError``
        to signal that this operation is not applicable.

        Parameters
        ----------
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    # ── Query operations (abstract core + concrete normalized surface) ────────

    @abstractmethod
    def get_vlans(self, device: Device, password: str) -> list[VLAN]:
        """Return VLANs configured on *device*.

        Backward-compatible entry point.  New code should call
        ``list_vlans()`` instead.

        Implementations must parse vendor-specific CLI output and normalize
        each entry into a ``VLAN`` object, excluding reserved / internal
        VLANs.

        Parameters
        ----------
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        list[VLAN]
            Normalized VLAN entries.

        Raises
        ------
        RuntimeError
            If the playbook fails or returns unparseable output.
        """

    def list_vlans(self, device: Device, password: str) -> list[VLAN]:
        """Normalized entry point for listing VLANs on *device*.

        Preferred over ``get_vlans()`` in new code.  The default
        implementation delegates to ``get_vlans()`` so that drivers which
        have not yet been updated continue to work transparently.  Drivers
        that override this method should in turn make ``get_vlans()`` delegate
        here to keep both names consistent.

        Parameters
        ----------
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        list[VLAN]
            Normalized VLAN entries.
        """
        return self.get_vlans(device, password)

    def get_vlan(self, vlan_id: int, device: Device, password: str) -> VLAN | None:
        """Return the ``VLAN`` for *vlan_id* on *device*, or ``None`` if absent.

        Default implementation performs a full ``list_vlans()`` scan.
        Drivers that support a more efficient single-item fetch may override
        this method.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier to look up.
        device:
            Domain device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        VLAN | None
            Matching VLAN entry, or ``None`` if not configured on the device.
        """
        return next(
            (v for v in self.list_vlans(device, password) if v.vlan_id == vlan_id),
            None,
        )
