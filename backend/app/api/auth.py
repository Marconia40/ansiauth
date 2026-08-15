from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm

from app.core.config import REFRESH_TOKEN_EXPIRE_DAYS, SSL_CERTFILE
from app.core.dependencies import require_role
from app.core.security import create_access_token
from app.schemas.auth import TokenResponse
from app.services import audit_service, login_attempt_service, refresh_token_service
from app.services.auth_service import authenticate_user

router = APIRouter()

_COOKIE_MAX_AGE = REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600
_COOKIE_SECURE = SSL_CERTFILE is not None


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="refresh_token",
        value=token,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="strict",
        max_age=_COOKIE_MAX_AGE,
        path="/",
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key="refresh_token", path="/")


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Login",
    description=(
        "Authenticate with username and password using OAuth2 form data. "
        "Returns a short-lived JWT access token. "
        "A rotating refresh token is set as an httpOnly cookie. "
        "Accounts are locked after repeated failures."
    ),
)
def login(request: Request, response: Response, form_data: OAuth2PasswordRequestForm = Depends()):
    ip = request.client.host if request.client else "unknown"
    username = form_data.username

    if login_attempt_service.is_ip_blocked(ip):
        audit_service.log_action(
            user=username,
            action="login",
            resource="auth",
            details={"username": username, "reason": "ip_blocked"},
            status="blocked",
        )
        raise HTTPException(status_code=429, detail="Too many requests")

    if login_attempt_service.is_username_locked(username):
        login_attempt_service.record_attempt(username, ip, succeeded=False)
        audit_service.log_action(
            user=username,
            action="login",
            resource="auth",
            details={"username": username, "reason": "account_locked"},
            status="blocked",
        )
        raise HTTPException(status_code=429, detail="Too many requests")

    user = authenticate_user(username, form_data.password)

    if not user:
        login_attempt_service.record_attempt(username, ip, succeeded=False)
        audit_service.log_action(
            user=username,
            action="login",
            resource="auth",
            details={"username": username},
            status="failed",
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    login_attempt_service.record_attempt(username, ip, succeeded=True)
    login_attempt_service.reset_username_failures(username)
    access_token = create_access_token({"sub": user.username, "role": user.role})
    refresh_token = refresh_token_service.create(user.username)
    audit_service.log_action(
        user=user.username,
        action="login",
        resource="auth",
        details={"username": user.username},
        status="success",
    )
    _set_refresh_cookie(response, refresh_token)
    return {"access_token": access_token, "token_type": "bearer", "refresh_token": refresh_token}


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh access token",
    description=(
        "Exchange the refresh token cookie for a new access token and a rotated refresh token cookie. "
        "The old refresh token is invalidated on use."
    ),
)
def refresh(request: Request, response: Response):
    raw = request.cookies.get("refresh_token")
    if not raw:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    try:
        new_refresh_token, username = refresh_token_service.validate_and_rotate(raw)
    except ValueError as exc:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail=str(exc))

    from app.services import user_service
    user = user_service.get_by_username(username)
    if user is None or not user.is_active:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access_token = create_access_token({"sub": user.username, "role": user.role})
    audit_service.log_action(
        user=user.username,
        action="token_refresh",
        resource="auth",
        details={"username": user.username},
        status="success",
    )
    _set_refresh_cookie(response, new_refresh_token)
    return {"access_token": access_token, "token_type": "bearer", "refresh_token": new_refresh_token}


@router.post(
    "/logout",
    status_code=200,
    summary="Logout",
    description="Revoke the refresh token cookie. Subsequent refresh attempts will return 401.",
)
def logout(request: Request, response: Response):
    raw = request.cookies.get("refresh_token")
    revoked = refresh_token_service.revoke(raw) if raw else False
    _clear_refresh_cookie(response)
    return {"success": True, "data": {"revoked": revoked}}


@router.post(
    "/unlock/{username}",
    status_code=200,
    summary="Unlock account",
    description=(
        "Clear all failed login attempts for a locked user account. "
        "Requires admin role or higher."
    ),
)
def unlock_account(username: str, current_user: dict = Depends(require_role("admin"))):
    count = login_attempt_service.unlock_username(username)
    audit_service.log_action(
        user=current_user["username"],
        action="unlock_account",
        resource="auth",
        details={"username": username, "attempts_cleared": count},
        status="success",
    )
    return {"success": True, "data": {"username": username, "attempts_cleared": count}}
