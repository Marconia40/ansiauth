import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class DeviceExecution:
    """Snapshot of a single device's execution state within a group job."""

    device: str
    job_id: Optional[str] = None
    status: str = "pending"
    current_step: Optional[str] = None
    retry_count: int = 0
    rollback_performed: bool = False
    rollback_success: Optional[bool] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "device": self.device,
            "job_id": self.job_id,
            "status": self.status,
            "current_step": self.current_step,
            "retry_count": self.retry_count,
            "rollback_performed": self.rollback_performed,
            "rollback_success": self.rollback_success,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DeviceExecution":
        return cls(
            device=d["device"],
            job_id=d.get("job_id"),
            status=d.get("status", "pending"),
            current_step=d.get("current_step"),
            retry_count=d.get("retry_count", 0),
            rollback_performed=d.get("rollback_performed", False),
            rollback_success=d.get("rollback_success"),
            error=d.get("error"),
            duration_ms=d.get("duration_ms"),
        )


@dataclass
class GroupJob:
    """One logical VLAN operation dispatched sequentially across N devices."""

    group_job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = "pending"
    operation: Optional[str] = None
    playbook: Optional[str] = None
    parameters: Optional[dict] = None
    device_results: list[DeviceExecution] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @property
    def total_devices(self) -> int:
        return len(self.device_results)

    def execution_summary(self) -> dict:
        completed = sum(1 for r in self.device_results if r.status == "completed")
        failed = sum(1 for r in self.device_results if r.status == "failed")
        rollback_count = sum(1 for r in self.device_results if r.rollback_performed)
        return {
            "total_devices": self.total_devices,
            "completed": completed,
            "failed": failed,
            "rollback_count": rollback_count,
        }
