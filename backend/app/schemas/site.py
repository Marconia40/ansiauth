from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _trim_name(v: str) -> str:
    trimmed = v.strip()
    if not trimmed:
        raise ValueError("name must not be empty")
    return trimmed


class SiteCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"name": "Library", "description": "Library building IDF closets"}
    })

    name: str = Field(..., min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v):
        return _trim_name(v)


class SiteUpdate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"description": "Updated description"}
    })

    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    description: Optional[str] = Field(default=None, max_length=255)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v):
        if v is None:
            return v
        return _trim_name(v)


class SiteRead(BaseModel):
    id: int
    name: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime
    device_count: int = 0
