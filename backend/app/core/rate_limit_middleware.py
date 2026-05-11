import threading
import time as _time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import RATE_LIMIT_LOGIN, RATE_LIMIT_PER_IP, RATE_LIMIT_PER_USER

# Module-level limits — reassignable in tests via monkeypatch.
LOGIN_RATE_LIMIT_RPM: int = RATE_LIMIT_LOGIN
RATE_LIMIT_PER_IP_RPM: int = RATE_LIMIT_PER_IP
RATE_LIMIT_PER_USER_RPM: int = RATE_LIMIT_PER_USER
WINDOW_SECONDS: float = 60.0

_buckets: dict[str, list[float]] = {}
_lock = threading.Lock()

_LOGIN_PATH = "/api/v1/auth/login"


def _get_now() -> float:
    return _time.time()


def reset() -> None:
    with _lock:
        _buckets.clear()


def _check_and_record(key: str, limit: int) -> tuple[bool, int]:
    now = _get_now()
    cutoff = now - WINDOW_SECONDS
    with _lock:
        bucket = _buckets.setdefault(key, [])
        _buckets[key] = [t for t in bucket if t > cutoff]
        if len(_buckets[key]) >= limit:
            oldest = min(_buckets[key])
            retry_after = max(int(oldest + WINDOW_SECONDS - now) + 1, 1)
            return False, retry_after
        _buckets[key].append(now)
        return True, 0


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        ip = request.client.host if request.client else "unknown"

        username = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                from app.core.security import verify_token
                payload = verify_token(auth_header[7:])
                username = payload.get("sub")
            except Exception:
                pass

        if request.url.path == _LOGIN_PATH:
            key = f"login:{ip}"
            limit = LOGIN_RATE_LIMIT_RPM
        elif username:
            key = f"user:{username}"
            limit = RATE_LIMIT_PER_USER_RPM
        else:
            key = f"ip:{ip}"
            limit = RATE_LIMIT_PER_IP_RPM

        allowed, retry_after = _check_and_record(key, limit)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)
