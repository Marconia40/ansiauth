from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.port import PortConfigResult, Puerto
    from app.models.vlan import VLAN


class VendorDriver(ABC):
    """Abstract base class every vendor driver must implement — VLAN and
    port operations fused into one contract (FINAL_ARCHITECTURE.md §1.6:
    ``Device.driver`` is a single property returning a single object, so a
    vendor can no longer be split across separate VLAN/port driver classes).

    All concrete drivers must satisfy this interface so that callers can
    remain fully vendor-agnostic.

    VLAN return-value contracts
    ----------------------------
    Mutation operations (create / update / delete / save_config):
        dict with keys:
            rc      – int  : Ansible return code (0 = success, non-zero = failure)
            stdout  – str  : combined playbook stdout
            stderr  – str  : combined playbook stderr
            success – bool : True iff rc == 0  (normalized convenience field)

    Query operations (list_vlans / get_vlans / get_vlan):
        list_vlans / get_vlans → list[VLAN]
        get_vlan               → VLAN | None

    Port query-operation contract
    ------------------------------
    ``list_ports(device, password)`` must return a normalized
    ``list[Puerto]``.  Implementations must:

    * filter out pseudo-interfaces (SVIs, loopbacks, NULL, ...);
    * never invent values — missing fields become ``None``;
    * raise ``RuntimeError`` if the playbook fails or returns unparseable
      output (callers convert this to a 502/500 at the API boundary).

    Port mutation operations default to raising ``NotImplementedError`` so
    vendors that haven't wired a given operation yet are explicit about the
    gap — the orchestration layer converts this into a controlled
    ``UnsupportedVendorError``/501 for the API client.
    """

    # ── VLAN mutation operations (must be implemented by every driver) ───────

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

    # ── VLAN query operations (abstract core + concrete normalized surface) ──

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

    # ── Port query operation (must be implemented by every driver) ───────────

    @abstractmethod
    def list_ports(self, device: Device, password: str) -> list[Puerto]:
        """Return the physical-port inventory of *device* as ``Puerto`` objects.

        Parameters
        ----------
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller before
            passing in).

        Returns
        -------
        list[Puerto]
            Normalized port entries, sorted by interface name.  Empty list
            when the device reports no physical interfaces.

        Raises
        ------
        RuntimeError
            If the underlying playbook fails or returns output that cannot
            be parsed.
        """

    # ── Port mutation operations (default: NotImplementedError stub) ─────────

    def update_port_description(
        self,
        interface: str,
        description: str,
        device: Device,
        password: str,
    ) -> dict:
        """Set the description of *interface* on *device*.

        Concrete drivers should override this; the default implementation
        raises ``NotImplementedError`` so vendors that haven't been wired
        yet are explicit about the gap. The orchestration layer converts
        this into a clean ``UnsupportedVendorError`` for the API client.

        Parameters
        ----------
        interface:
            Vendor-native interface name (e.g. ``"GigabitEthernet1/0/1"``).
        description:
            New description text.  An empty string requests the driver to
            clear the description (``undo description`` on Huawei VRP,
            ``no description`` on Cisco IOS).
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            Normalized Ansible result:
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement update_port_description yet"
        )

    def set_port_admin_state(
        self,
        interface: str,
        enabled: bool,
        device: Device,
        password: str,
    ) -> dict:
        """Administratively enable or disable *interface* on *device*.

        Concrete drivers should override this; the default implementation
        raises ``NotImplementedError`` so vendors that haven't been wired
        yet are explicit about the gap, and the API layer can present a
        clean ``VENDOR_NOT_SUPPORTED`` 501 instead of leaking the stub.

        Vendor mapping:
            * Huawei VRP — ``undo shutdown`` (enable) / ``shutdown`` (disable)
              inside the interface view, followed by ``commit``.
            * Cisco IOS  — ``no shutdown`` / ``shutdown`` inside
              ``interface <name>`` parent context.

        Parameters
        ----------
        interface:
            Vendor-native interface name (e.g. ``"GigabitEthernet1/0/1"``).
        enabled:
            ``True``  → bring the interface up (``undo shutdown`` /
            ``no shutdown``).
            ``False`` → bring the interface down (``shutdown``).
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            Normalized Ansible result:
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_port_admin_state yet"
        )

    def set_port_access_vlan(
        self,
        interface: str,
        vlan_id: int,
        device: Device,
        password: str,
    ) -> dict:
        """Assign *vlan_id* as the access VLAN on *interface*.

        Concrete drivers should override this; the default raises
        ``NotImplementedError`` so the API layer returns a controlled 501.

        **Pre-conditions are the caller's responsibility:** the
        orchestration layer must verify the port is in access mode and
        the VLAN id is valid *before* invoking this method.  Drivers
        execute the change directly without checking mode — they trust
        the caller's pre-state validation.

        Vendor mapping:
            * Huawei VRP — ``port default vlan <id>`` inside the
              interface view, followed by ``commit``.
            * Cisco IOS  — ``switchport access vlan <id>`` inside
              ``interface <name>`` parent context.

        Parameters
        ----------
        interface:
            Vendor-native interface name (e.g. ``"GigabitEthernet1/0/1"``).
        vlan_id:
            New access VLAN identifier (1–4094, excluding 1002–1005).
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_port_access_vlan yet"
        )

    def set_trunk_pvid_vlan(
        self,
        interface: str,
        vlan_id: int,
        device: Device,
        password: str,
    ) -> dict:
        """Set the trunk native VLAN (PVID) of *interface* on *device*.

        Used when the port is in trunk mode to set the untagged/native VLAN.
        The orchestration layer guarantees the port is in trunk mode before
        invoking this method.

        Vendor mapping:
            * Huawei VRP — ``port trunk pvid vlan <id>`` inside the interface
              view, followed by ``commit``.
            * Cisco IOS  — ``switchport trunk native vlan <id>`` inside
              ``interface <name>`` parent context.

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_trunk_pvid_vlan yet"
        )

    def set_trunk_allowed_vlans(
        self,
        interface: str,
        vlan_list: list[int],
        device: Device,
        password: str,
    ) -> dict:
        """Set the trunk allowed-VLAN list of *interface* to exactly *vlan_list*.

        The driver always performs a full replace (clear existing + set
        desired), not a delta.  The orchestration layer is responsible for
        computing the desired list from the requested mode (replace / add /
        remove) and the pre-state — the driver receives only the final list.

        **Pre-conditions are the caller's responsibility:** the orchestration
        layer must verify the port is in trunk mode and *vlan_list* is valid
        before invoking this method.

        Vendor mapping:
            * Huawei VRP — ``undo port trunk allow-pass vlan all`` followed by
              ``port trunk allow-pass vlan <list>`` inside the interface view,
              then ``commit``.
            * Cisco IOS  — ``switchport trunk allowed vlan <list>`` inside
              ``interface <name>`` parent context with ``save_when: always``.

        Parameters
        ----------
        interface:
            Vendor-native interface name.
        vlan_list:
            Sorted, deduplicated list of VLAN IDs to allow on the trunk.
            Must be non-empty and validated by the caller.
        device:
            Domain device object.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement set_trunk_allowed_vlans yet"
        )

    def configure_port(
        self,
        config: Puerto,
        device: Device,
        password: str,
    ) -> PortConfigResult:
        """Apply a composite set of port mutations in a single driver call.

        Concrete drivers should override it to apply all fields in *config*
        that are non-``None`` in a single device interaction.  The default
        raises ``NotImplementedError`` so vendors that haven't wired this
        yet surface a controlled ``VENDOR_NOT_SUPPORTED`` 501 rather than
        a bare exception.

        The driver is responsible for:
        * Respecting the field ordering (e.g. set mode before VLAN).
        * Returning a ``PortConfigResult`` that reflects what actually changed.
        * Raising ``RuntimeError`` (not ``NotImplementedError``) if a playbook
          fails mid-operation so the orchestration layer can trigger rollback.

        Parameters
        ----------
        config:
            Validated ``Puerto`` — callers must pass an already-validated
            instance (``validar()`` already called); the driver may trust
            its invariants.
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        PortConfigResult
            Normalized result describing success, change status, and
            optional rollback / warning metadata.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement configure_port yet"
        )

    def shutdown_port(
        self,
        interface: str,
        device: Device,
        password: str,
    ) -> dict:
        """Administratively disable *interface* on *device* (shutdown).

        Semantic wrapper: equivalent to ``set_port_admin_state(enabled=False)``
        but expressed as an explicit, intent-named operation so higher-level
        orchestration and audit logs can describe the action unambiguously.

        Concrete drivers should override this; the default raises
        ``NotImplementedError`` so vendors without an implementation surface
        a controlled 501 at the API boundary.

        Vendor mapping (informational — implementations fill in the details):
            * Huawei VRP — ``shutdown`` inside interface view + ``commit``.
            * Cisco IOS  — ``shutdown`` inside ``interface <name>`` context.

        Parameters
        ----------
        interface:
            Vendor-native interface name (e.g. ``"GigabitEthernet1/0/1"``).
        device:
            Domain device object.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            Normalized Ansible result:
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement shutdown_port yet"
        )

    def enable_port(
        self,
        interface: str,
        device: Device,
        password: str,
    ) -> dict:
        """Administratively enable *interface* on *device* (no shutdown).

        Semantic wrapper: equivalent to ``set_port_admin_state(enabled=True)``
        but expressed as an explicit, intent-named operation.

        Concrete drivers should override this; the default raises
        ``NotImplementedError`` so vendors without an implementation surface
        a controlled 501 at the API boundary.

        Vendor mapping (informational — implementations fill in the details):
            * Huawei VRP — ``undo shutdown`` inside interface view + ``commit``.
            * Cisco IOS  — ``no shutdown`` inside ``interface <name>`` context.

        Parameters
        ----------
        interface:
            Vendor-native interface name.
        device:
            Domain device object.
        password:
            Plaintext device password (decrypted by the caller).

        Returns
        -------
        dict
            Normalized Ansible result:
            ``{"rc": int, "stdout": str, "stderr": str, "success": bool}``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement enable_port yet"
        )

    def get_port(self, name: str, device: Device, password: str) -> Puerto | None:
        """Return the ``Puerto`` for *name* on *device*, or ``None`` if absent.

        Default implementation performs a full ``list_ports()`` scan.
        Drivers that support a more efficient single-port fetch may override.

        Parameters
        ----------
        name:
            Interface identifier (e.g. ``"GigabitEthernet0/0/1"``).
        device:
            Domain device object.
        password:
            Plaintext device password.

        Returns
        -------
        Puerto | None
        """
        return next(
            (p for p in self.list_ports(device, password) if p.interface == name),
            None,
        )
