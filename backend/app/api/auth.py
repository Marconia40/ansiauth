from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm

from app.core.dependencies import require_role
from app.core.security import create_access_token
from app.schemas.auth import LogoutRequest, RefreshRequest, TokenResponse
from app.services import audit_service, login_attempt_service, refresh_token_service
from app.services.auth_service import authenticate_user

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
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
    return {"access_token": access_token, "token_type": "bearer", "refresh_token": refresh_token}


@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshRequest):
    try:
        new_refresh_token, username = refresh_token_service.validate_and_rotate(body.refresh_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    from app.services import user_service
    user = user_service.get_by_username(username)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    access_token = create_access_token({"sub": user.username, "role": user.role})
    audit_service.log_action(
        user=user.username,
        action="token_refresh",
        resource="auth",
        details={"username": user.username},
        status="success",
    )
    return {"access_token": access_token, "token_type": "bearer", "refresh_token": new_refresh_token}


@router.post("/logout", status_code=200)
def logout(body: LogoutRequest):
    revoked = refresh_token_service.revoke(body.refresh_token)
    return {"success": True, "data": {"revoked": revoked}}


@router.post("/unlock/{username}", status_code=200)
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
