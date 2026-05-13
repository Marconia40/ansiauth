import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Device:
    name: str
    host: str
    vendor: str
    username: str
    encrypted_password: str
    platform: str = "ios"
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
