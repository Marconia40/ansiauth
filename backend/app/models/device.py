from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from app.services.vendors.base import VendorDriver


@dataclass
class Device:
    """Identidad de un device administrado (site, grupo, credenciales) —
    expone su driver de vendor y su password resueltos y cacheados, no
    ejecuta nada por sí mismo."""
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
    _driver: "VendorDriver | None" = field(default=None, repr=False, compare=False, init=False)
    _password: "str | None" = field(default=None, repr=False, compare=False, init=False)

    @property
    def driver(self) -> "VendorDriver":
        if self._driver is None:
            from app.composition import plugin_registry  # import local -- ver FASE_1.md A3
            self._driver = plugin_registry.obtener(self.vendor)
        return self._driver

    @property
    def password(self) -> str:
        if self._password is None:
            from app.composition import secret_vault  # import local -- ver FASE_1.md A3
            self._password = secret_vault.decrypt(self.encrypted_password)
        return self._password
