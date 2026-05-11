from pydantic import BaseModel


class TokenResponse(BaseModel):
    access_token: str
    token_type: str
    refresh_token: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str
