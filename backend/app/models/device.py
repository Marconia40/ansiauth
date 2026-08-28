import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


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
