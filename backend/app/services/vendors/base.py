from __future__ import annotations

from abc import ABC, abstractmethod


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
        list_vlans → list of dicts, each: {"vlan_id": int, "name": str}
        get_vlan   → {"vlan_id": int, "name": str} | None
    """

    # ── Mutation operations (must be implemented by every driver) ────────────

    @abstractmethod
    def create_vlan(self, vlan_id: int, name: str, device, password: str) -> dict:
        """Provision a new VLAN on the target device.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier (1–4094).
        name:
            Human-readable label to assign to the VLAN.
        device:
            ORM device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    @abstractmethod
    def delete_vlan(self, vlan_id: int, device, password: str) -> dict:
        """Remove an existing VLAN from the target device.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier of the VLAN to remove.
        device:
            ORM device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    @abstractmethod
    def update_vlan(self, vlan_id: int, name: str, device, password: str) -> dict:
        """Rename / update the description of an existing VLAN.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier of the VLAN to update.
        name:
            New label to assign to the VLAN.
        device:
            ORM device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    @abstractmethod
    def save_config(self, device, password: str) -> dict:
        """Persist the running configuration to non-volatile storage.

        On platforms where changes are committed automatically (e.g. some
        Cisco IOS versions), implementations should raise ``NotImplementedError``
        to signal that this operation is not applicable.

        Parameters
        ----------
        device:
            ORM device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``
        """

    # ── Query operations (abstract core + concrete normalized surface) ────────

    @abstractmethod
    def get_vlans(self, device, password: str) -> list[dict]:
        """Return VLANs configured on *device*.

        This method satisfies the abstract contract and exists for backward
        compatibility.  New code should call ``list_vlans()`` instead.

        Implementations must parse vendor-specific CLI output and normalize
        every entry to ``{"vlan_id": int, "name": str}``, excluding
        reserved / internal VLANs.

        Parameters
        ----------
        device:
            ORM device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        list[dict]
            List of ``{"vlan_id": int, "name": str}`` entries.

        Raises
        ------
        RuntimeError
            If the playbook fails or returns unparseable output.
        """

    def list_vlans(self, device, password: str) -> list[dict]:
        """Normalized entry point for listing VLANs on *device*.

        Preferred over ``get_vlans()`` in new code.  The default
        implementation delegates to ``get_vlans()`` so that drivers which
        have not yet been updated continue to work transparently.  Drivers
        that override this method should in turn make ``get_vlans()`` delegate
        here to keep both names consistent.

        Parameters
        ----------
        device:
            ORM device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        list[dict]
            List of ``{"vlan_id": int, "name": str}`` entries.
        """
        return self.get_vlans(device, password)

    def get_vlan(self, vlan_id: int, device, password: str) -> dict | None:
        """Return the entry for *vlan_id* on *device*, or ``None`` if absent.

        Default implementation performs a full ``list_vlans()`` scan.
        Drivers that support a more efficient single-item fetch may override
        this method.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier to look up.
        device:
            ORM device object exposing .name, .host, .username.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict | None
            ``{"vlan_id": int, "name": str}`` if the VLAN exists, else ``None``.
        """
        return next(
            (v for v in self.list_vlans(device, password) if v["vlan_id"] == vlan_id),
            None,
        )
