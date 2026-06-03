"""Background cleanup routines for tables and on-disk artefacts that would
otherwise grow without bound.

Each function is independently callable so tests can exercise them directly
and the scheduler can wire them up one by one. They all log a one-line INFO
summary and return the count of items removed so callers can attach metrics
or audit events later.
"""
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone

from app.core.config import ANSIBLE_BASE_PATH
from app.db.models import LoginAttemptModel, RefreshTokenModel
from app.db.session import get_session

logger = logging.getLogger(__name__)


def purge_old_artifacts(retention_days: int = 30) -> int:
    """Remove ansible-runner artifact directories older than ``retention_days``.

    Each playbook invocation creates a UUID-named subdirectory under
    ``ANSIBLE_BASE_PATH/artifacts``. Directories whose mtime is older than the
    cutoff are deleted recursively. Returns the count of directories removed.
    """
    artifacts_dir = os.path.join(ANSIBLE_BASE_PATH, "artifacts")
    if not os.path.isdir(artifacts_dir):
        logger.info("Artifact purge skipped: %s does not exist", artifacts_dir)
        return 0

    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=retention_days)).timestamp()
    removed = 0
    for entry in os.scandir(artifacts_dir):
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff_ts:
                shutil.rmtree(entry.path)
                removed += 1
        except OSError as exc:
            # Best-effort: a sibling process may have removed the directory
            # between scandir and rmtree. Log and continue.
            logger.warning("Failed to remove artifact %s: %s", entry.path, exc)

    logger.info(
        "Artifact purge complete: removed=%d retention_days=%d dir=%s",
        removed, retention_days, artifacts_dir,
    )
    return removed


def sweep_expired_refresh_tokens() -> int:
    """Delete refresh_tokens rows whose ``expires_at`` is in the past.

    Includes already-revoked rows — once a refresh token is past its TTL it
    can never be redeemed again, so it serves no further purpose. Returns the
    number of rows deleted.
    """
    now = datetime.now(timezone.utc)
    with get_session() as session:
        deleted = (
            session.query(RefreshTokenModel)
            .filter(RefreshTokenModel.expires_at < now)
            .delete(synchronize_session=False)
        )
    logger.info("Refresh-token sweep complete: deleted=%d", deleted)
    return deleted


def sweep_old_login_attempts(retention_days: int = 7) -> int:
    """Delete login_attempts rows older than ``retention_days``.

    The lockout window itself is shorter (minutes/hours, governed by
    ``login_attempt_service``) so this sweep only affects historical rows that
    no longer influence the rate-limit / lockout decision. Returns the number
    of rows deleted.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    with get_session() as session:
        deleted = (
            session.query(LoginAttemptModel)
            .filter(LoginAttemptModel.attempted_at < cutoff)
            .delete(synchronize_session=False)
        )
    logger.info(
        "Login-attempt sweep complete: deleted=%d retention_days=%d",
        deleted, retention_days,
    )
    return deleted


def run_all(
    artifact_retention_days: int = 30,
    login_attempt_retention_days: int = 7,
) -> dict:
    """Run every cleanup routine in sequence and return a summary dict.

    Exceptions from any single routine are caught and logged so a transient
    failure (e.g. a permission error wiping one artifact directory) does not
    skip the others.
    """
    summary: dict = {}
    for name, func in (
        ("artifacts", lambda: purge_old_artifacts(artifact_retention_days)),
        ("refresh_tokens", sweep_expired_refresh_tokens),
        ("login_attempts", lambda: sweep_old_login_attempts(login_attempt_retention_days)),
    ):
        try:
            summary[name] = func()
        except Exception:
            logger.exception("Cleanup routine %r failed", name)
            summary[name] = None
    return summary
