from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm

from app.core.security import create_access_token
from app.schemas.auth import TokenResponse
from app.services import audit_service
from app.services.auth_service import authenticate_user

router = APIRouter()


@router.post("/login", response_model=TokenResponse)
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        audit_service.log_action(
            user=form_data.username,
            action="login",
            resource="auth",
            details={"username": form_data.username},
            status="failed",
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"sub": user.username, "role": user.role})
    audit_service.log_action(
        user=user.username,
        action="login",
        resource="auth",
        details={"username": user.username},
        status="success",
    )
    return {"access_token": token, "token_type": "bearer"}
