from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm

from app.core.config import COOKIE_SAMESITE, COOKIE_SECURE, REFRESH_TOKEN_EXPIRE_MINUTES
from app.core.scope import require_system_admin
from app.core.security import create_access_token
from app.models.audit import AuditRecord
from app.schemas.auth import TokenResponse
from app.services import refresh_token_service
from app.services.auth_service import authenticate_user

router = APIRouter()

_COOKIE_MAX_AGE = REFRESH_TOKEN_EXPIRE_MINUTES * 60


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key="refresh_token",
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
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

    from app.composition import audit_repository, login_attempt_repository

    if login_attempt_repository.ip_bloqueada(ip):
        audit_repository.append(AuditRecord(
            user=username,
            action="login",
            resource="auth",
            details={"username": username, "reason": "ip_blocked"},
            status="blocked",
        ))
        raise HTTPException(status_code=429, detail="Too many requests")

    if login_attempt_repository.esta_bloqueado(username):
        login_attempt_repository.registrar_intento(username, ip, exitoso=False)
        audit_repository.append(AuditRecord(
            user=username,
            action="login",
            resource="auth",
            details={"username": username, "reason": "account_locked"},
            status="blocked",
        ))
        raise HTTPException(status_code=429, detail="Too many requests")

    user = authenticate_user(username, form_data.password)

    if not user:
        login_attempt_repository.registrar_intento(username, ip, exitoso=False)
        audit_repository.append(AuditRecord(
            user=username,
            action="login",
            resource="auth",
            details={"username": username},
            status="failed",
        ))
        raise HTTPException(status_code=401, detail="Invalid credentials")

    login_attempt_repository.registrar_intento(username, ip, exitoso=True)
    login_attempt_repository.resetear(username)
    access_token = create_access_token({"sub": user.username, "is_system_admin": user.is_system_admin})
    refresh_token = refresh_token_service.create(user.username)
    audit_repository.append(AuditRecord(
        user=user.username,
        action="login",
        resource="auth",
        details={"username": user.username},
        status="success",
    ))
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

    from app.composition import user_repository
    user = user_repository.obtener_por_username(username)
    if user is None or not user.is_active:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access_token = create_access_token({"sub": user.username, "is_system_admin": user.is_system_admin})
    from app.composition import audit_repository
    audit_repository.append(AuditRecord(
        user=user.username,
        action="token_refresh",
        resource="auth",
        details={"username": user.username},
        status="success",
    ))
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
        "**MSP Phase 3 (ESC-4):** promoted from admin → system-admin — "
        "unlock has no site scope and is treated as a system-level operation "
        "(consistent with D26 activation/deactivation semantics)."
    ),
)
def unlock_account(
    username: str,
    current_user: dict = Depends(require_system_admin),
):
    from app.composition import audit_repository, login_attempt_repository

    count = login_attempt_repository.resetear(username)
    audit_repository.append(AuditRecord(
        user=current_user["username"],
        action="unlock_account",
        resource="auth",
        details={"username": username, "attempts_cleared": count},
        status="success",
    ))
    return {"success": True, "data": {"username": username, "attempts_cleared": count}}
