from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


_VALID_ROLES = {"super-admin", "admin", "operator", "observer"}


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8)
    role: str = Field(default="observer")
    email: Optional[str] = None

    def model_post_init(self, __context):
        if self.role not in _VALID_ROLES:
            raise ValueError(f"role must be one of {_VALID_ROLES}")


class UserRead(BaseModel):
    id: int
    username: str
    email: Optional[str]
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserUpdate(BaseModel):
    email: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8)

    def model_post_init(self, __context):
        if self.role is not None and self.role not in _VALID_ROLES:
            raise ValueError(f"role must be one of {_VALID_ROLES}")
