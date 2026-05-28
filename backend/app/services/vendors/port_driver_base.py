from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.device import Device
    from app.models.port import PortInfo


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
