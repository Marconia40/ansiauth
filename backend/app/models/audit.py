from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class AuditRecord(BaseModel):
    id: str
    timestamp: datetime
    user: str
    action: str
    resource: str
    resource_id: Optional[str] = None
    details: dict
    status: str
    job_id: Optional[str] = None
