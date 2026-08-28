"""MSP: Phase 6 — request-scoped user context for Postgres RLS policies.

Every RLS policy installed by ``msp_rls`` reads two Postgres GUCs:

  * ``app.user_id``           — integer, primary key of the calling user
  * ``app.is_system_admin``   — boolean, true when the caller bypasses scope

The GUCs are set via ``SET LOCAL`` inside every transaction (so they never
leak across requests or connections in the pool). This module owns the
plumbing:

  1. ``current_user_ctx`` — a ``ContextVar`` populated by the HTTP middleware
     (``rls_middleware.RLSSessionMiddleware``) or by explicit calls to
     :func:`system_context` for internal machinery (bootstrap, Celery tasks).
  2. :func:`system_context` — a context manager that marks the current
     execution scope as "trusted internal" so the ``after_begin`` hook sets
     ``is_system_admin=true``. Used by startup bootstrap and by the Celery
     worker (via :func:`install_worker_system_context`).
  3. :func:`install_session_rls_hook` — SQLAlchemy ``after_begin`` listener
     that runs the ``SET LOCAL`` statements on every Postgres transaction.
     Called once from ``init_db`` right after the sessionmaker is built.

Design notes
------------

* ``SET LOCAL`` is transaction-scoped, so nothing leaks when a connection
  returns to the pool.
* The default (no context set anywhere) is ``user_id = 0``, ``is_system_admin
  = false`` — every RLS policy that references these GUCs then denies. This
  is the safe default: any code path that reaches the DB without going
  through either the request middleware or an explicit ``system_context()``
  is treated as unauthenticated.
* SQLite has no RLS; the hook detects the dialect and is a no-op there so
  dev environments and the pytest suite still work.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional, TypedDict

from sqlalchemy import event, text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class UserContext(TypedDict, total=False):
    id: Optional[int]
    is_system_admin: bool


# Set by ``rls_middleware.RLSSessionMiddleware`` at the start of each request.
# Never set outside that middleware or :func:`system_context`. Default {} means
# "unauthenticated" → the after_begin hook writes (user_id=0, is_system_admin=
# false), which every RLS policy interprets as deny.
current_user_ctx: ContextVar[UserContext] = ContextVar(
    "current_user_ctx", default={}
)

# Distinct from ``current_user_ctx`` so bootstrap code cannot accidentally
# leak an "authenticated" identity — it only asserts "trusted internal".
_system_context_active: ContextVar[bool] = ContextVar(
    "_system_context_active", default=False
)


@contextmanager
def system_context():
    """Mark the enclosed block as trusted internal machinery.

    Sets ``is_system_admin=true`` in the after_begin hook regardless of any
    authenticated user context. Use for bootstrap (``ensure_base_infrastructure``,
    ``_bootstrap_admin``, ``seed_defaults``) and for background tasks that run
    without a JWT (Celery workers).
    """
    token = _system_context_active.set(True)
    try:
        yield
    finally:
        _system_context_active.reset(token)


def resolve_current_gucs() -> tuple[int, bool]:
    """Return the ``(user_id, is_system_admin)`` pair the after_begin hook
    will write for the current context. Exposed for tests + diagnostics."""
    if _system_context_active.get():
        return (0, True)
    user = current_user_ctx.get() or {}
    uid = user.get("id") or 0
    is_sys = bool(user.get("is_system_admin", False))
    return (int(uid), is_sys)


def install_session_rls_hook(session_factory) -> None:
    """Register the ``after_begin`` listener on the app's sessionmaker.

    Called from :func:`app.db.session.init_db` right after the sessionmaker
    is created. Idempotent — safe to call once per process.
    """

    @event.listens_for(session_factory, "after_begin")
    def _apply_rls_gucs(session: Session, _transaction, connection):
        if connection is None or connection.dialect.name != "postgresql":
            return
        uid, is_sys = resolve_current_gucs()
        # SET LOCAL is transaction-scoped, so this cannot leak when the
        # connection is returned to the pool.
        connection.execute(
            text("SELECT set_config('app.user_id', :uid, true)"),
            {"uid": str(uid)},
        )
        connection.execute(
            text("SELECT set_config('app.is_system_admin', :sa, true)"),
            {"sa": "true" if is_sys else "false"},
        )

    logger.debug("RLS session hook installed")


def install_worker_system_context() -> None:
    """Pin the current process to :func:`system_context` semantics forever.

    Called by the Celery worker's ``worker_process_init`` signal so that
    every DB access from a background task is treated as trusted internal
    (Celery tasks have no JWT). Not a context manager — the effect persists
    for the process lifetime.
    """
    _system_context_active.set(True)
    logger.info("RLS: worker process pinned to system context")
