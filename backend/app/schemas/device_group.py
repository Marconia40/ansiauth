from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DeviceGroupCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"name": "core-switches", "description": "Core layer switches at HQ", "site_id": 1}
    })

    name: str = Field(..., min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=255)
    # site_id is required for new groups (step 7.4). Legacy groups created before
    # this step may still exist with site_id=NULL and are admin-only.
    site_id: int = Field(..., ge=1)

    @field_validator("name")
    @classmethod
    def _trim_name(cls, v: str) -> str:
        # Same rule as SiteCreate -- without it, "Core" and "  Core  " pass
        # device_group_repository.existe()'s exact-string uniqueness check
        # as two different names despite displaying identically.
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("name must not be empty")
        return trimmed


class DeviceGroupRead(BaseModel):
    id: int
    name: str
    description: Optional[str]
    created_at: datetime
    member_count: int = 0
    site_id: Optional[int] = None
    site_name: Optional[str] = None
