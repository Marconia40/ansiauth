"""Application-layer immutability guard for ``audit_logs`` rows.

Replaces the SQLite-only ``audit_log_immutable`` BEFORE UPDATE trigger that
was installed at startup until Step 1, then migrated under Alembic in Step 1.
A trigger is portable only when every supported backend supports the same
syntax — SQLite uses ``RAISE(ABORT, …)``, Postgres needs a PL/pgSQL function
plus a trigger, MySQL needs SIGNAL. Doing the check at the ORM layer
sidesteps that fragmentation: every session goes through this listener
regardless of dialect.

Semantics intentionally match the previous SQLite trigger:

* **UPDATE is blocked.** Any attempt to mutate a persisted ``AuditLogModel``
  raises :class:`AuditImmutabilityError` before the SQL is issued. INSERT
  (the only legitimate write path) is unaffected.
* **DELETE is allowed.** ``purge_old_records`` (retention policy) and
  ``clear_audit_log`` (test fixture) need it, and the previous trigger
  didn't block it either. Step 16's hash chain will provide the
  tamper-detection layer that catches deletions.
"""
from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.db.models import AuditLogModel


class AuditImmutabilityError(RuntimeError):
    """Raised when something tries to UPDATE an existing audit_logs row."""


def _block_updates(session: Session, flush_context, instances) -> None:  # noqa: ARG001
    """``before_flush`` listener — refuses to flush dirty audit rows.

    ``session.dirty`` may contain instances that aren't actually modified
    (e.g. rows loaded but only touched), so we double-check with
    ``session.is_modified`` to keep the false-positive rate at zero.
    """
    for obj in session.dirty:
        if isinstance(obj, AuditLogModel) and session.is_modified(obj, include_collections=False):
            raise AuditImmutabilityError(
                f"audit_logs row id={obj.id} is immutable — "
                "append a new row via AuditRepository.append() instead "
                "(set parent_audit_id to chain it to this one)"
            )


def install() -> None:
    """Register the guard on the SQLAlchemy ``Session`` class.

    Idempotent: re-installing in the same process is a no-op so the function
    is safe to call from both ``init_db`` and test fixtures.
    """
    if not event.contains(Session, "before_flush", _block_updates):
        event.listen(Session, "before_flush", _block_updates)
