from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.port import PortConfigRequest, PortConfigResult, PortInfo


class BasePortDriver(ABC):
    """Abstract base class every vendor port driver must implement.

    Mirrors the contract style used by ``BaseVendorDriver`` (VLANs) so that
    the service layer can dispatch port operations vendor-agnostically.

    Step 1.1 scope
    --------------
    Read-only.  Only ``list_ports`` is exposed.  Mutation operations
    (enable/disable, description change, VLAN assignment, etc.) are
    intentionally out of scope and will be added in a later step.

    Query-operation contract
    ------------------------
    ``list_ports(device, password)`` must return a normalized
    ``list[PortInfo]``.  Implementations must:

    * filter out pseudo-interfaces (SVIs, loopbacks, NULL, ...);
    * never invent values — missing fields become ``None``;
    * raise ``RuntimeError`` if the playbook fails or returns unparseable
      output (callers convert this to a 502/500 at the API boundary).
    """

    @abstractmethod
    def list_ports(self, device: Device, password: str) -> list[PortInfo]:
        """Return the physical-port inventory of *device* as ``PortInfo`` objects.

        Parameters
        ----------
        device:
            Domain device object exposing ``.name``, ``.host``, ``.username``.
        password:
            Plaintext device password (decrypted by the caller before
            passing in).

        Returns
        -------
        list[PortInfo]
            Normalized port entries, sorted by interface name.  Empty list
            when the device reports no physical interfaces.

        Raises
        ------
        RuntimeError
            If the underlying playbook fails or returns output that cannot
            be parsed.
        """

    def update_port_description(
        self,
        interface: str,
        description: str,
        device: Device,
        password: str,
    ) -> dict:
        """Set the description of *interface* on *device*.

        Step 2.1 introduces the first port mutation operation.  Concrete
        drivers should override this; the default implementation raises
        ``NotImplementedError`` so vendors that haven't been wired yet are
        explicit about the gap.  The orchestration layer converts this
        into a clean ``UnsupportedVendorError`` for the API client.

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

        Step 2.2 introduces the second port mutation operation.  Concrete
        drivers should override this; the default implementation raises
        ``NotImplementedError`` so vendors that haven't been wired yet are
        explicit about the gap, and the API layer can present a clean
        ``VENDOR_NOT_SUPPORTED`` 501 instead of leaking the stub.

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

        Step 2.3 introduces the third port mutation operation.  Concrete
        drivers should override this; the default raises
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

    def set_trunk_allowed_vlans(
        self,
        interface: str,
        vlan_list: list[int],
        device: Device,
        password: str,
    ) -> dict:
        """Set the trunk allowed-VLAN list of *interface* to exactly *vlan_list*.

        Step 2.4 introduces the fourth port mutation operation.  The driver
        always performs a full replace (clear existing + set desired), not a
        delta.  The orchestration layer is responsible for computing the
        desired list from the requested mode (replace / add / remove) and the
        pre-state — the driver receives only the final list.

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
        config: PortConfigRequest,
        device: Device,
        password: str,
    ) -> PortConfigResult:
        """Apply a composite set of port mutations in a single driver call.

        Step 3.1 registers this method as part of the vendor contract.
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
            Validated ``PortConfigRequest`` — callers must pass an already-
            validated instance; the driver may trust its invariants.
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

        Step 3.1 registers this method as part of the vendor contract.
        Concrete drivers should override it; the default raises
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

        Step 3.1 registers this method as part of the vendor contract.
        Concrete drivers should override it; the default raises
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

    def get_port(self, name: str, device: Device, password: str) -> PortInfo | None:
        """Return the ``PortInfo`` for *name* on *device*, or ``None`` if absent.

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
        PortInfo | None
        """
        return next(
            (p for p in self.list_ports(device, password) if p.name == name),
            None,
        )
