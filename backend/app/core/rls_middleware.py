"""MSP: Phase 6 — unified auth-context + rate-limit middleware.

Originally two independent middlewares in two separate files —
``RLSSessionMiddleware`` (RLS context) and ``RateLimitMiddleware``
(``core/rate_limit_middleware.py``, now folded into this one) — each
decoding the same JWT on its own (a 3rd decode happened again in
``core/scope.py: get_current_user()``) and each querying ``users``
separately (this middleware for the RLS id, ``require_authenticated()``
for ``is_active``/``is_system_admin``). Merged into one class, one file,
that decodes the JWT once and queries ``users`` once per request.

Merging also fixes a real ordering bug, found while tracing this for
docs/migracion-final-architecture's sequence diagrams: Starlette's
``add_middleware()`` does ``self.user_middleware.insert(0, ...)`` — the
middleware added *first* ends up executing *last* (confirmed with a real
Starlette ``TestClient`` run, not just reading the source). The old
``RLSSessionMiddleware`` was added *before* ``RateLimitMiddleware`` in
``main.py``, which — because of ``insert(0, ...)`` — actually made it run
*after* the rate limiter, not before. That contradicted this module's own
former docstring ("added before rate limiting so a 429 response path still
runs under a well-defined context"): a 429 from the old
``RateLimitMiddleware`` short-circuited before ``RLSSessionMiddleware``
ever ran, so that response path never had RLS context installed. Harmless
today (rate limiting doesn't touch the DB), but a trap for whoever adds
audit logging to that path later (there's precedent —
``app.main: request_validation_error_handler`` already does this for
422s). Being one middleware now, there is no "order between two
middlewares" left to get wrong.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from typing import Optional

from jwt import PyJWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.config import RATE_LIMIT_LOGIN, RATE_LIMIT_PER_IP, RATE_LIMIT_PER_USER
from app.core.rls_context import UserContext, current_user_ctx
from app.core.security import verify_token

logger = logging.getLogger(__name__)


# ─── Rate limiting — state + helpers ───────────────────────────────────────
# Was core/rate_limit_middleware.py (its own file) — folded in here since
# it's only ever driven from AuthContextMiddleware.dispatch() now. Kept as
# module-level state/functions (not instance state on the class) because
# tests reach in directly: `from app.core import rls_middleware as rl;
# rl.reset()`.

# Reassignable in tests via monkeypatch.
LOGIN_RATE_LIMIT_RPM: int = RATE_LIMIT_LOGIN
RATE_LIMIT_PER_IP_RPM: int = RATE_LIMIT_PER_IP
RATE_LIMIT_PER_USER_RPM: int = RATE_LIMIT_PER_USER
WINDOW_SECONDS: float = 60.0

_buckets: dict[str, list[float]] = {}
_lock = threading.Lock()

_LOGIN_PATH = "/api/v1/auth/login"
_EXCLUDED_PATHS: frozenset[str] = frozenset({"/health"})


def is_excluded(path: str) -> bool:
    return path in _EXCLUDED_PATHS


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


def bucket_for(request: Request, username: "str | None") -> tuple[str, int]:
    """Pick the rate-limit bucket key + limit for *request*. *username* is
    whatever the caller already decoded (payload["sub"]), not lowercased —
    bucket keys were never case-normalized."""
    ip = request.client.host if request.client else "unknown"
    if request.url.path == _LOGIN_PATH:
        return f"login:{ip}", LOGIN_RATE_LIMIT_RPM
    if username:
        return f"user:{username}", RATE_LIMIT_PER_USER_RPM
    return f"ip:{ip}", RATE_LIMIT_PER_IP_RPM


# ─── Auth context (JWT decode + RLS) ────────────────────────────────────────

def decode_bearer(request: Request) -> "tuple[Optional[dict], Optional[str]]":
    """Best-effort JWT decode — never raises.

    Returns ``(payload, None)`` on success or ``(None, <401 detail>)`` on
    failure. Single source of truth for the 3 distinct 401 reasons
    ``core/scope.py: get_current_user()`` surfaces to callers — kept here
    (not duplicated) so ``AuthContextMiddleware`` and
    ``get_current_user()``'s defensive fallback agree on the exact wording.
    """
    auth = request.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None, "Not authenticated"
    token = auth.split(" ", 1)[1].strip()
    try:
        payload = verify_token(token)
    except PyJWTError:
        return None, "Invalid or expired token"
    if not payload.get("sub"):
        return None, "Invalid token payload"
    return payload, None


def _lookup_user(username: str) -> "tuple[int, bool, bool] | None":
    """Resolve *username* to ``(id, is_active, is_system_admin)``.

    Used for both the RLS ``app.user_id`` GUC (only ``id`` matters there)
    and ``require_authenticated()``'s DB-authoritative check (needs all
    three) — one query instead of the two separate ones each side used to
    run before this middleware existed. Runs inside its own session under
    ``system_context()`` so the lookup is not itself subject to the RLS
    context we're about to install with its result.
    """
    from app.core.rls_context import system_context
    from app.db.models import UserModel
    from app.db.session import get_session

    try:
        with system_context():
            with get_session() as session:
                row = (
                    session.query(UserModel.id, UserModel.is_active, UserModel.is_system_admin)
                    .filter(UserModel.username == username.lower())
                    .first()
                )
                return (int(row[0]), bool(row[1]), bool(row[2])) if row is not None else None
    except Exception:  # pragma: no cover — defensive: never break the request
        logger.warning("AuthContextMiddleware: failed to resolve user id for %s", username)
        return None


def _to_rls_ctx(payload: "dict | None", user_id: "int | None") -> UserContext:
    """Build the RLS ``UserContext``.

    ``is_system_admin`` here comes from the **JWT claim** (with the legacy
    ``role`` fallback), not the DB column — matches the pre-merge
    ``RLSSessionMiddleware._decode_authenticated_user()`` exactly. This is
    deliberately a different source than ``require_authenticated()``'s
    DB-authoritative ``is_system_admin`` (see ``core/scope.py``) — RLS
    trusts the token's claim, business-logic authorization trusts the DB.
    """
    if payload is None:
        return {}
    is_sys = payload.get("is_system_admin")
    if is_sys is None:
        is_sys = (payload.get("role") or "").lower() in {"admin", "super-admin"}
    return {"id": user_id, "is_system_admin": bool(is_sys)}


class AuthContextMiddleware(BaseHTTPMiddleware):
    """Populates auth/RLS context and enforces the rate limit, once, for
    every request. See module docstring for why this replaces the old
    ``RLSSessionMiddleware`` + ``RateLimitMiddleware`` pair.

    Sets on ``request.state`` (read by ``core/scope.py``'s
    ``get_current_user()``/``require_authenticated()``):
      * ``auth_resolved``  — always ``True`` once this middleware has run.
      * ``auth_payload``   — decoded JWT payload, or ``None``.
      * ``auth_error``     — the 401 detail string if decode failed, else ``None``.
      * ``auth_user_row``  — ``(id, is_active, is_system_admin)`` from the DB, or ``None``.
    """

    async def dispatch(self, request: Request, call_next):
        payload, error = decode_bearer(request)
        username = payload.get("sub") if payload else None
        user_row = _lookup_user(username) if username else None

        request.state.auth_resolved = True
        request.state.auth_payload = payload
        request.state.auth_error = error
        request.state.auth_user_row = user_row

        rls_token = current_user_ctx.set(
            _to_rls_ctx(payload, user_row[0] if user_row is not None else None)
        )
        try:
            if not is_excluded(request.url.path):
                key, limit = bucket_for(request, username)
                allowed, retry_after = _check_and_record(key, limit)
                if not allowed:
                    from app.schemas.error import make_error
                    from starlette.responses import JSONResponse

                    return JSONResponse(
                        status_code=429,
                        content=make_error(429, "Too many requests", "RATE_LIMIT_EXCEEDED"),
                        headers={"Retry-After": str(retry_after)},
                    )
            return await call_next(request)
        finally:
            current_user_ctx.reset(rls_token)
