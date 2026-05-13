from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class DeviceGroupCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"name": "core-switches", "description": "Core layer switches at HQ"}
    })

    name: str = Field(..., min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=255)


class DeviceGroupRead(BaseModel):
    id: int
    name: str
    description: Optional[str]
    created_at: datetime
    member_count: int = 0


class DeviceGroupMemberCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"device_name": "switch-01"}
    })

    device_name: str
