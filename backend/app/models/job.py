import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class Job:
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "pending"
    playbook: Optional[str] = None
    device: Optional[str] = None
    parameters: Optional[dict] = None
    result: Optional[dict] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    retry_count: int = 0
    max_retries: int = 3
    rollback_performed: bool = False
    rollback_success: Optional[bool] = None
    pre_state: Optional[dict] = None
    last_error: Optional[str] = None
    current_step: Optional[str] = None
    group_job_id: Optional[str] = None
