from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.role_assignment import RoleAssignmentCreate


class UserCreate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "username": "operator1",
            "password": "securepassword1",
            "email": "operator1@example.com",
            "is_system_admin": False,
            "initial_grant": {"site_id": 1, "role": "observer"},
        }
    })

    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8)
    email: Optional[str] = None
    is_system_admin: bool = False
    # Optional for system-admin callers (they can create bare users and add
    # grants later); required for site-admin callers so the created user is
    # visible to them under the scoped list filter.
    initial_grant: Optional[RoleAssignmentCreate] = None


class UserRead(BaseModel):
    id: int
    username: str
    email: Optional[str]
    is_active: bool
    is_system_admin: bool
    created_at: datetime
    updated_at: datetime


class UserUpdate(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {"email": "newmail@example.com", "is_active": True}
    })

    email: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=8)
