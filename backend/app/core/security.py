from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from app.core.config import ACCESS_TOKEN_EXPIRE_MINUTES, JWT_SECRET_KEY

if not JWT_SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY must be set in .env")

if ACCESS_TOKEN_EXPIRE_MINUTES <= 0 or ACCESS_TOKEN_EXPIRE_MINUTES > 60:
    raise RuntimeError(
        f"ACCESS_TOKEN_EXPIRE_MINUTES must be between 1 and 60, got: {ACCESS_TOKEN_EXPIRE_MINUTES}"
    )

ALGORITHM = "HS256"

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET_KEY, algorithms=[ALGORITHM])
