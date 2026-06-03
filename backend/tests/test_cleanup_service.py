"""Tests for backend/app/services/cleanup_service.py — Step 13.1 §3."""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import LoginAttemptModel, RefreshTokenModel
from app.db.session import get_session
from app.services import cleanup_service


# ── purge_old_artifacts ──────────────────────────────────────────────────────

@pytest.fixture
def fake_artifact_root(tmp_path, monkeypatch):
    """Point cleanup_service at a temporary artifacts directory.

    Patches ANSIBLE_BASE_PATH on the cleanup_service module (the module
    captured a reference at import time) so the function operates on a
    disposable tree rather than the real one under backend/ansible/.
    """
    monkeypatch.setattr(cleanup_service, "ANSIBLE_BASE_PATH", str(tmp_path))
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return artifacts


def _make_artifact_dir(root, name: str, age_days: float) -> str:
    """Create a UUID-shaped subdir under root and backdate its mtime."""
    d = root / name
    d.mkdir()
    # Write a sentinel file so the rmtree path exercises a non-empty dir.
    (d / "stdout").write_text("simulated playbook output\n")
    ts = time.time() - age_days * 86400
    os.utime(d, (ts, ts))
    return str(d)


def test_purge_old_artifacts_removes_only_aged_dirs(fake_artifact_root):
    old = _make_artifact_dir(fake_artifact_root, "old-aaa", age_days=45)
    recent = _make_artifact_dir(fake_artifact_root, "recent-bbb", age_days=2)

    removed = cleanup_service.purge_old_artifacts(retention_days=30)

    assert removed == 1
    assert not os.path.exists(old)
    assert os.path.exists(recent)


def test_purge_old_artifacts_nothing_to_delete(fake_artifact_root):
    _make_artifact_dir(fake_artifact_root, "fresh-aaa", age_days=1)
    _make_artifact_dir(fake_artifact_root, "fresh-bbb", age_days=10)

    removed = cleanup_service.purge_old_artifacts(retention_days=30)

    assert removed == 0
    assert len(list(fake_artifact_root.iterdir())) == 2


def test_purge_old_artifacts_missing_directory_is_noop(tmp_path, monkeypatch):
    # Point at a path with no artifacts/ subdirectory — common on fresh
    # deployments before the first playbook has ever run.
    monkeypatch.setattr(cleanup_service, "ANSIBLE_BASE_PATH", str(tmp_path))
    assert cleanup_service.purge_old_artifacts(retention_days=30) == 0


# ── sweep_expired_refresh_tokens ─────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_tokens_and_attempts():
    with get_session() as session:
        session.query(RefreshTokenModel).delete(synchronize_session=False)
        session.query(LoginAttemptModel).delete(synchronize_session=False)
    yield
    with get_session() as session:
        session.query(RefreshTokenModel).delete(synchronize_session=False)
        session.query(LoginAttemptModel).delete(synchronize_session=False)


def _seed_refresh_token(username: str, expires_at: datetime) -> None:
    with get_session() as session:
        session.add(
            RefreshTokenModel(
                token_hash=f"hash-{username}-{expires_at.isoformat()}",
                username=username,
                expires_at=expires_at,
                revoked=False,
                created_at=datetime.now(timezone.utc),
            )
        )


def test_sweep_expired_refresh_tokens_deletes_past_expiry():
    now = datetime.now(timezone.utc)
    _seed_refresh_token("alice", now - timedelta(hours=1))
    _seed_refresh_token("bob", now - timedelta(days=2))
    _seed_refresh_token("carol", now + timedelta(hours=1))  # still valid

    deleted = cleanup_service.sweep_expired_refresh_tokens()

    assert deleted == 2
    with get_session() as session:
        remaining = [row.username for row in session.query(RefreshTokenModel).all()]
    assert remaining == ["carol"]


def test_sweep_expired_refresh_tokens_nothing_to_delete():
    now = datetime.now(timezone.utc)
    _seed_refresh_token("dan", now + timedelta(hours=4))
    _seed_refresh_token("erin", now + timedelta(days=10))

    deleted = cleanup_service.sweep_expired_refresh_tokens()

    assert deleted == 0
    with get_session() as session:
        assert session.query(RefreshTokenModel).count() == 2


# ── sweep_old_login_attempts ─────────────────────────────────────────────────

def _seed_login_attempt(username: str, attempted_at: datetime, succeeded: bool = False) -> None:
    with get_session() as session:
        session.add(
            LoginAttemptModel(
                username=username,
                ip_address="1.2.3.4",
                attempted_at=attempted_at,
                succeeded=succeeded,
            )
        )


def test_sweep_old_login_attempts_deletes_aged_rows():
    now = datetime.now(timezone.utc)
    _seed_login_attempt("ghost", now - timedelta(days=10))
    _seed_login_attempt("ghost", now - timedelta(days=8))
    _seed_login_attempt("recent", now - timedelta(days=2))

    deleted = cleanup_service.sweep_old_login_attempts(retention_days=7)

    assert deleted == 2
    with get_session() as session:
        remaining = [row.username for row in session.query(LoginAttemptModel).all()]
    assert remaining == ["recent"]


def test_sweep_old_login_attempts_nothing_to_delete():
    now = datetime.now(timezone.utc)
    _seed_login_attempt("a", now - timedelta(hours=1))
    _seed_login_attempt("b", now - timedelta(days=3))

    deleted = cleanup_service.sweep_old_login_attempts(retention_days=7)

    assert deleted == 0
    with get_session() as session:
        assert session.query(LoginAttemptModel).count() == 2


# ── run_all aggregator ───────────────────────────────────────────────────────

def test_run_all_summarises_each_routine(fake_artifact_root):
    now = datetime.now(timezone.utc)
    _make_artifact_dir(fake_artifact_root, "old-a", age_days=45)
    _seed_refresh_token("expired", now - timedelta(hours=1))
    _seed_login_attempt("oldlogin", now - timedelta(days=10))

    summary = cleanup_service.run_all(
        artifact_retention_days=30,
        login_attempt_retention_days=7,
    )

    assert summary == {"artifacts": 1, "refresh_tokens": 1, "login_attempts": 1}
