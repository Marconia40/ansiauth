from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm

from app.core.config import (
    COOKIE_SAMESITE,
    COOKIE_SECURE,
    ELEVATED_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_MINUTES,
)
from app.core.response import ok
from app.core.scope import require_authenticated, require_system_admin
from app.core.security import create_access_token, create_elevated_token
from app.models.audit import AuditRecord
from app.schemas.auth import (
    ActiveSessionsResponse,
    ReauthRequest,
    ReauthResponse,
    TokenResponse,
)
from app.services import refresh_token_service
from app.services.auth_service import authenticate_user

router = APIRouter()

_COOKIE_MAX_AGE = REFRESH_TOKEN_EXPIRE_MINUTES * 60

# Map service-level ``ValueError`` sentinels to stable ``detail`` codes the
# frontend switches on (idle vs replay vs absolute → distinct banners). Any
# unmapped string collapses to "invalid" so we never leak internals.
_REFRESH_ERROR_DETAIL = {
    refresh_token_service.ERR_IDLE: "idle_timeout",
    refresh_token_service.ERR_ABSOLUTE: "session_absolute_limit",
    refresh_token_service.ERR_REPLAY: "replay_detected",
    refresh_token_service.ERR_EXPIRED: "expired",
    refresh_token_service.ERR_INVALID: "invalid",
}


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent") or None


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
    """Todas las filas de auditoría de este archivo (login/refresh/unlock)
    se quedan en el camino directo ``audit_repository.append()``, no pasan
    por ``EventDispatcher``/``DomainEvent`` -- ``resource="auth"`` es un
    sentinel que ``AuditRepository._aplicar_scope()`` matchea literal, no
    corresponde a ninguna clase de dominio real, y varios de estos eventos
    (login bloqueado/fallido) auditan un request que falló antes de tocar
    ninguna entidad -- no hay ``recurso`` real que pasarle a
    ``DomainEvent``. Mismo criterio que ``jobs.py:cancel_job``."""
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
    access_token = create_access_token({"sub": user.username, "id": user.id, "is_system_admin": user.is_system_admin})
    refresh_token = refresh_token_service.create(
        user.username,
        ip_address=_client_ip(request),
        user_agent=_user_agent(request),
    )
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
        new_refresh_token, username, _session_id = refresh_token_service.validate_and_rotate(
            raw,
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
    except ValueError as exc:
        _clear_refresh_cookie(response)
        detail = _REFRESH_ERROR_DETAIL.get(str(exc), "invalid")
        # Audit the failure with the reason so operators can see idle/replay
        # cuts in the log without decoding the response.
        from app.composition import audit_repository
        audit_repository.append(AuditRecord(
            user="unknown",
            action="token_refresh",
            resource="auth",
            details={"reason": detail},
            status="failed",
        ))
        raise HTTPException(status_code=401, detail=detail)

    from app.composition import user_repository
    user = user_repository.obtener_por_username(username)
    if user is None or not user.is_active:
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access_token = create_access_token({"sub": user.username, "id": user.id, "is_system_admin": user.is_system_admin})
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
    return ok({"revoked": revoked})


@router.post(
    "/reauth",
    response_model=ReauthResponse,
    summary="Step-up re-authentication",
    description=(
        "Verify the caller's password and return a short-lived elevated "
        "token. Required by irreversible endpoints (delete user, promote/"
        "demote system-admin, revoke grant, delete site) so an unattended "
        "browser cannot chain destructive actions without a fresh password "
        "check. Attach the returned token as the ``X-Elevated-Auth`` header."
    ),
)
def reauth(
    request: Request,
    body: ReauthRequest,
    current_user: dict = Depends(require_authenticated),
):
    from app.composition import audit_repository

    username = current_user["username"]
    user = authenticate_user(username, body.password)
    if not user:
        audit_repository.append(AuditRecord(
            user=username,
            action="reauth",
            resource="auth",
            details={"username": username},
            status="failed",
        ))
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_elevated_token(user.username)
    audit_repository.append(AuditRecord(
        user=user.username,
        action="reauth",
        resource="auth",
        details={"username": user.username},
        status="success",
    ))
    return {
        "elevated_token": token,
        "expires_in": ELEVATED_TOKEN_EXPIRE_MINUTES * 60,
    }


@router.get(
    "/sessions",
    response_model=ActiveSessionsResponse,
    summary="List active sessions",
    description=(
        "Return one entry per active session for the current user, grouped "
        "by session_id (rotations of the same login collapse into a single "
        "row). The session that owns the caller's refresh cookie is "
        "flagged with ``current=true`` so the UI can render 'this device' "
        "and skip it when offering to revoke the rest."
    ),
)
def list_sessions(
    request: Request,
    current_user: dict = Depends(require_authenticated),
):
    raw = request.cookies.get("refresh_token")
    current_session_id = (
        refresh_token_service.get_session_id_for_raw(raw) if raw else None
    )
    sessions = refresh_token_service.list_active_sessions(
        current_user["username"],
        current_session_id=current_session_id,
    )
    return {"sessions": sessions}


@router.post(
    "/sessions/revoke-others",
    status_code=200,
    summary="Revoke every session except the current one",
    description=(
        "Revoke every active refresh-token row that does not belong to the "
        "session the caller is currently using. Useful when a user "
        "suspects an old device still has a live session and wants to "
        "force it out without changing the password."
    ),
)
def revoke_other_sessions(
    request: Request,
    current_user: dict = Depends(require_authenticated),
):
    raw = request.cookies.get("refresh_token")
    current_session_id = (
        refresh_token_service.get_session_id_for_raw(raw) if raw else None
    )
    if current_session_id is None:
        # Without a session_id we would revoke every session including the
        # caller's own — which is just /logout with extra steps. Force the
        # caller to have a valid refresh cookie so the "keep this one"
        # decision is unambiguous.
        raise HTTPException(status_code=400, detail="Missing current session")

    revoked = refresh_token_service.revoke_other_sessions(
        current_user["username"], current_session_id
    )
    from app.composition import audit_repository
    audit_repository.append(AuditRecord(
        user=current_user["username"],
        action="revoke_other_sessions",
        resource="auth",
        details={"revoked_rows": revoked, "kept_session_id": current_session_id},
        status="success",
    ))
    return ok({"revoked": revoked})


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
    return ok({"username": username, "attempts_cleared": count})
