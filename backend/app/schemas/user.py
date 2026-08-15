from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


_VALID_ROLES = {"super-admin", "admin", "operator", "observer"}


class UserCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "username": "operator1",
            "password": "securepassword1",
            "role": "operator",
            "email": "operator1@example.com",
            "allowed_site_ids": [1, 2],
        }
    })

    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8)
    role: str = Field(default="observer")
    email: Optional[str] = None
    allowed_site_ids: Optional[list[int]] = None

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
    allowed_site_ids: list[int] = []
    allowed_site_names: list[str] = []


class UserUpdate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"email": "newmail@example.com", "role": "admin", "allowed_site_ids": [1]}
    })

    email: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8)
    # When present (even as []), the allowed_sites set is replaced. Omitting it
    # leaves the existing set untouched.
    allowed_site_ids: Optional[list[int]] = None

    def model_post_init(self, __context):
        if self.role is not None and self.role not in _VALID_ROLES:
            raise ValueError(f"role must be one of {_VALID_ROLES}")
