from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TokenResponse(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "example": {
            "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
            "token_type": "bearer",
            "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
        }
    })

    access_token: str
    token_type: str
    refresh_token: str


class ActiveSession(BaseModel):
    """A single active session (one row per session_id chain) as returned
    by ``GET /auth/sessions``. ``current=True`` marks the row that owns
    the refresh cookie the caller sent, so the UI can render "this
    device" and avoid offering to revoke it separately."""
    session_id: str
    current: bool
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    ip_address: str | None
    user_agent: str | None


class ActiveSessionsResponse(BaseModel):
    sessions: list[ActiveSession]
