"""Tests for the first-run admin bootstrap (USR-005)."""
import importlib
import os

import pytest

from app.db.models import AuditLogModel, UserModel
from app.db.session import get_session


@pytest.fixture(autouse=True)
def clean_users_and_audit():
    with get_session() as session:
        session.query(UserModel).delete()
    yield
    with get_session() as session:
        session.query(UserModel).delete()
        session.query(AuditLogModel).filter_by(action="bootstrap_admin").delete()


def _run_bootstrap(monkeypatch, user="admin", password=None):
    """Patch env vars, then call the bootstrap function from main directly."""
    import app.main as main_module

    if password is not None:
        monkeypatch.setattr("app.core.config.BOOTSTRAP_ADMIN_USER", user)
        monkeypatch.setattr("app.core.config.BOOTSTRAP_ADMIN_PASSWORD", password)
    else:
        monkeypatch.setattr("app.core.config.BOOTSTRAP_ADMIN_PASSWORD", None)

    main_module._bootstrap_admin()


# ── Happy path ────────────────────────────────────────────────────────────────

def test_bootstrap_creates_admin_on_empty_db(monkeypatch):
    _run_bootstrap(monkeypatch, user="firstadmin", password="strongpassword1")

    with get_session() as session:
        row = session.query(UserModel).filter_by(username="firstadmin").first()
        assert row is not None
        assert row.is_system_admin is True
        assert row.is_active is True


def test_bootstrap_writes_audit_log(monkeypatch):
    _run_bootstrap(monkeypatch, user="auditadmin", password="strongpassword1")

    with get_session() as session:
        entry = session.query(AuditLogModel).filter_by(action="bootstrap_admin").first()
        assert entry is not None
        assert entry.user == "system"
        assert entry.status == "success"
        assert entry.details["username"] == "auditadmin"


def test_bootstrap_idempotent_skips_if_admin_exists(monkeypatch):
    _run_bootstrap(monkeypatch, user="admin1", password="strongpassword1")
    _run_bootstrap(monkeypatch, user="admin2", password="strongpassword2")

    with get_session() as session:
        count = session.query(UserModel).filter_by(is_system_admin=True).count()
    assert count == 1  # second call must not create a second admin


def test_bootstrap_uses_custom_username(monkeypatch):
    _run_bootstrap(monkeypatch, user="netops", password="strongpassword1")

    with get_session() as session:
        row = session.query(UserModel).filter_by(is_system_admin=True).first()
        assert row is not None
        assert row.username == "netops"


# ── Guard: no password ────────────────────────────────────────────────────────

def test_bootstrap_no_password_env_logs_warning_and_skips(monkeypatch, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="app.main"):
        _run_bootstrap(monkeypatch, password=None)

    with get_session() as session:
        count = session.query(UserModel).filter_by(is_system_admin=True).count()
    assert count == 0
    assert any("BOOTSTRAP_ADMIN_PASSWORD" in r.message for r in caplog.records)


# ── Guard: weak password ──────────────────────────────────────────────────────

def test_bootstrap_weak_password_raises_runtime_error(monkeypatch):
    with pytest.raises(RuntimeError, match="at least 12"):
        _run_bootstrap(monkeypatch, password="short")


def test_bootstrap_password_exactly_12_chars_accepted(monkeypatch):
    _run_bootstrap(monkeypatch, user="borderline", password="exactly12chr")

    with get_session() as session:
        row = session.query(UserModel).filter_by(username="borderline").first()
    assert row is not None
