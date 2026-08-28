"""MSP: Phase 6 — HTTP middleware that populates the RLS user context.

Decodes the ``Authorization: Bearer <jwt>`` header (best-effort — invalid /
missing tokens leave the context empty, which the after_begin hook translates
to a deny-by-default RLS predicate). Look up the caller's ``id`` in the DB
so the Postgres policy — which JOINs ``role_assignments`` on ``user_id`` —
has a real primary key to match against.

Wired into ``app.main`` before rate limiting and CORS so the context is set
for every downstream handler and error path.
"""
from __future__ import annotations

import logging
from typing import Optional

from jwt import PyJWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.rls_context import UserContext, current_user_ctx
from app.core.security import verify_token

logger = logging.getLogger(__name__)


class RLSSessionMiddleware(BaseHTTPMiddleware):
    """Populate ``current_user_ctx`` for the duration of each request.

    The context is used by the SQLAlchemy ``after_begin`` hook in
    :mod:`app.core.rls_context` to run ``SET LOCAL app.user_id`` and
    ``app.is_system_admin`` on every Postgres transaction. The context
    resets when the middleware returns, so nothing leaks between requests
    even on the same connection.
    """

    async def dispatch(self, request: Request, call_next):
        user_ctx = _decode_authenticated_user(request)
        token = current_user_ctx.set(user_ctx)
        try:
            response = await call_next(request)
        finally:
            current_user_ctx.reset(token)
        return response


def _decode_authenticated_user(request: Request) -> UserContext:
    """Best-effort JWT decode. Returns ``{}`` on any failure — the after_begin
    hook then applies the deny-by-default GUC values."""
    auth_header = request.headers.get("Authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        return {}
    token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return {}
    try:
        payload = verify_token(token)
    except PyJWTError:
        return {}
    username = payload.get("sub")
    if not username:
        return {}
    is_sys = payload.get("is_system_admin")
    if is_sys is None:
        # Legacy JWT compat — same fallback as core.scope.get_current_user.
        is_sys = (payload.get("role") or "").lower() in {"admin", "super-admin"}
    uid = _lookup_user_id(str(username).lower())
    return {"id": uid, "is_system_admin": bool(is_sys)}


def _lookup_user_id(username: str) -> Optional[int]:
    """Resolve a username to its ``users.id`` for the RLS ``app.user_id`` GUC.

    Runs inside its own session so the resolution is not itself subject to
    the RLS context we're about to install. Returns ``None`` if the row is
    missing — the after_begin hook will then write ``user_id = 0`` which
    causes every policy's grant-lookup subquery to return no rows (deny).
    """
    # Import inside the function so this module can be loaded before init_db
    # runs (e.g. during test collection).
    from app.core.rls_context import system_context
    from app.db.models import UserModel
    from app.db.session import get_session

    try:
        with system_context():
            with get_session() as session:
                row = (
                    session.query(UserModel.id)
                    .filter(UserModel.username == username)
                    .first()
                )
                return int(row[0]) if row is not None else None
    except Exception:  # pragma: no cover — defensive: never break the request
        logger.warning("RLS middleware: failed to resolve user id for %s", username)
        return None
