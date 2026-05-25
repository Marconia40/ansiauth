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
            rc     – int   : Ansible return code (0 = success, non-zero = failure)
            stdout – str   : combined playbook stdout
            stderr – str   : combined playbook stderr

    Query operations (get_vlans):
        list of dicts, each with keys:
            vlan_id – int : numeric VLAN identifier
            name    – str : human-readable VLAN label
    """

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
            ORM device object providing .name, .host, .username attributes.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            Ansible execution result: ``{"rc": int, "stdout": str, "stderr": str}``.
        """

    @abstractmethod
    def delete_vlan(self, vlan_id: int, device, password: str) -> dict:
        """Remove an existing VLAN from the target device.

        Parameters
        ----------
        vlan_id:
            Numeric VLAN identifier of the VLAN to remove.
        device:
            ORM device object providing .name, .host, .username attributes.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            Ansible execution result: ``{"rc": int, "stdout": str, "stderr": str}``.
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
            ORM device object providing .name, .host, .username attributes.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            Ansible execution result: ``{"rc": int, "stdout": str, "stderr": str}``.
        """

    @abstractmethod
    def get_vlans(self, device, password: str) -> list[dict]:
        """Retrieve the list of VLANs currently configured on the target device.

        Implementations must parse vendor-specific CLI output and normalize
        each entry to ``{"vlan_id": int, "name": str}``.

        Parameters
        ----------
        device:
            ORM device object providing .name, .host, .username attributes.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        list[dict]
            List of ``{"vlan_id": int, "name": str}`` entries, excluding
            reserved/internal VLANs.

        Raises
        ------
        RuntimeError
            If the playbook fails or returns unparseable output.
        """

    @abstractmethod
    def save_config(self, device, password: str) -> dict:
        """Persist the running configuration to non-volatile storage.

        On platforms where the running config is automatically committed
        (e.g. some Cisco IOS versions), implementations may raise
        ``NotImplementedError`` to signal that this operation is not
        applicable, or return a no-op success result.

        Parameters
        ----------
        device:
            ORM device object providing .name, .host, .username attributes.
        password:
            Plaintext device password (decrypted by caller before passing in).

        Returns
        -------
        dict
            Ansible execution result: ``{"rc": int, "stdout": str, "stderr": str}``.
        """
