from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWTError

from app.core.security import verify_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    try:
        payload = verify_token(token)
    except PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    username = payload.get("sub")
    if not username:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    is_sys = payload.get("is_system_admin")
    if is_sys is None:
        is_sys = (payload.get("role") or "").lower() in {"admin", "super-admin"}
    return {"username": username, "is_system_admin": bool(is_sys)}
