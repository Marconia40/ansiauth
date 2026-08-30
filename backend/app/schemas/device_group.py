from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class DeviceGroupCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"name": "core-switches", "description": "Core layer switches at HQ", "site_id": 1}
    })

    name: str = Field(..., min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=255)
    # site_id is required for new groups (step 7.4). Legacy groups created before
    # this step may still exist with site_id=NULL and are admin-only.
    site_id: int = Field(..., ge=1)


class DeviceGroupRead(BaseModel):
    id: int
    name: str
    description: Optional[str]
    created_at: datetime
    member_count: int = 0
    site_id: Optional[int] = None
    site_name: Optional[str] = None
