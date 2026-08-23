from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.models.port import PortConfigRequest, PortConfigResult, PortInfo
    from app.models.vlan import VLAN
    from app.services.vendors.base import BaseVendorDriver
    from app.services.vendors.port_driver_base import BasePortDriver


@dataclass
class Device:
    """Domain model for a managed network device."""
    name: str
    host: str
    vendor: str
    username: str
    encrypted_password: str
    platform: str = "ios"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    site_id: Optional[int] = None
    site_name: Optional[str] = None
    # MSP: Phase 3 — the authoritative owning group. Populated by
    # ``device_service._to_domain`` from ``DeviceModel.device_group`` when
    # available; ``site_id``/``site_name`` above are derived from the group
    # when the MSP flag is on, and from the legacy row otherwise.
    device_group_id: Optional[int] = None
    device_group_name: Optional[str] = None

    # Lazily-resolved collaborators — not persisted, not part of __init__.
    _vlan_driver: "BaseVendorDriver | None" = field(default=None, repr=False, compare=False, init=False)
    _port_driver: "BasePortDriver | None" = field(default=None, repr=False, compare=False, init=False)
    _password: "str | None" = field(default=None, repr=False, compare=False, init=False)

    # ── driver/password resolution — internal, lazy, cached after first call ──
    def _get_vlan_driver(self) -> "BaseVendorDriver":
        if self._vlan_driver is None:
            from app.core.config import EXECUTION_MODE
            if EXECUTION_MODE == "mock":
                from app.services.vendors.mock import MockVlanDriver
                self._vlan_driver = MockVlanDriver()
            else:
                from app.services.vendors.dispatcher import get_driver
                self._vlan_driver = get_driver(self)
        return self._vlan_driver

    def _get_port_driver(self) -> "BasePortDriver":
        if self._port_driver is None:
            from app.core.config import EXECUTION_MODE
            if EXECUTION_MODE == "mock":
                from app.services.vendors.mock import MockPortDriver
                self._port_driver = MockPortDriver()
            else:
                from app.services.vendors.dispatcher import get_port_driver
                self._port_driver = get_port_driver(self)
        return self._port_driver

    def _get_password(self) -> "str | None":
        if self._password is None:
            from app.core.config import EXECUTION_MODE
            if EXECUTION_MODE != "mock":
                from app.services.secret_service import vault
                self._password = vault.decrypt(self.encrypted_password)
        return self._password

    # ── VLAN ──
    def create_vlan(self, vlan: "VLAN") -> dict:
        vlan.validate_name()
        return self._get_vlan_driver().create_vlan(vlan.vlan_id, vlan.name, self, self._get_password())

    def delete_vlan(self, vlan: "VLAN") -> dict:
        return self._get_vlan_driver().delete_vlan(vlan.vlan_id, self, self._get_password())

    def update_vlan_description(self, vlan: "VLAN") -> dict:
        vlan.validate_name()
        return self._get_vlan_driver().update_vlan(vlan.vlan_id, vlan.name, self, self._get_password())

    def list_vlans(self) -> "list[VLAN]":
        return self._get_vlan_driver().list_vlans(self, self._get_password())

    def save_config(self) -> dict:
        return self._get_vlan_driver().save_config(self, self._get_password())

    # ── Port ──
    def configure_port(self, config: "PortConfigRequest") -> "PortConfigResult":
        return self._get_port_driver().configure_port(config, self, self._get_password())

    def set_port_admin_state(self, interface: str, enabled: bool) -> dict:
        return self._get_port_driver().set_port_admin_state(interface, enabled, self, self._get_password())

    def set_port_access_vlan(self, interface: str, vlan_id: int) -> dict:
        return self._get_port_driver().set_port_access_vlan(interface, vlan_id, self, self._get_password())

    def set_trunk_allowed_vlans(self, interface: str, vlan_ids: "list[int]") -> dict:
        return self._get_port_driver().set_trunk_allowed_vlans(interface, vlan_ids, self, self._get_password())

    def update_port_description(self, interface: str, description: str) -> dict:
        return self._get_port_driver().update_port_description(interface, description, self, self._get_password())

    def list_ports(self) -> "list[PortInfo]":
        return self._get_port_driver().list_ports(self, self._get_password())
