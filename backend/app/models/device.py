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
