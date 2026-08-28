"""Tests for AUD-004 — audit log retention policy."""
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import AuditLogModel
from app.db.session import get_session
from app.services import audit_service


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_audit():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


def _insert_record(days_ago: int, action: str = "test_action") -> int:
    """Insert an audit record backdated by days_ago and return its DB id."""
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with get_session() as session:
        row = AuditLogModel(
            timestamp=ts,
            user="testuser",
            action=action,
            resource="test",
            status="success",
            details={},
        )
        session.add(row)
        session.flush()
        return row.id


# ── purge_old_records() service function ─────────────────────────────────────

def test_purge_deletes_records_older_than_retention():
    _insert_record(days_ago=100)
    _insert_record(days_ago=95)

    deleted = audit_service.purge_old_records(retention_days=90, triggered_by="test")
    assert deleted == 2

    with get_session() as session:
        count = session.query(AuditLogModel).filter_by(action="test_action").count()
    assert count == 0


def test_purge_preserves_records_within_window():
    _insert_record(days_ago=5)
    _insert_record(days_ago=89)

    deleted = audit_service.purge_old_records(retention_days=90, triggered_by="test")
    assert deleted == 0

    with get_session() as session:
        count = session.query(AuditLogModel).filter_by(action="test_action").count()
    assert count == 2


def test_purge_mixed_old_and_new():
    _insert_record(days_ago=200)
    _insert_record(days_ago=10)
    _insert_record(days_ago=91)

    deleted = audit_service.purge_old_records(retention_days=90, triggered_by="test")
    assert deleted == 2

    with get_session() as session:
        remaining = session.query(AuditLogModel).filter_by(action="test_action").all()
    assert len(remaining) == 1


def test_purge_empty_table_returns_zero():
    deleted = audit_service.purge_old_records(retention_days=90, triggered_by="test")
    assert deleted == 0


def test_purge_logs_system_audit_event():
    _insert_record(days_ago=100)
    audit_service.purge_old_records(retention_days=90, triggered_by="admin_user")

    with get_session() as session:
        entry = session.query(AuditLogModel).filter_by(action="audit_purge").first()
        assert entry is not None
        assert entry.user == "system"
        assert entry.details["triggered_by"] == "admin_user"
        assert entry.details["deleted_count"] == 1
        assert entry.details["retention_days"] == 90
        assert entry.status == "success"


def test_purge_logs_even_when_nothing_deleted(monkeypatch):
    """Manually triggered purge is always logged even if 0 rows deleted."""
    deleted = audit_service.purge_old_records(retention_days=90, triggered_by="admin")

    with get_session() as session:
        entry = session.query(AuditLogModel).filter_by(action="audit_purge").first()
    assert entry is not None
    assert deleted == 0


def test_purge_does_not_log_when_scheduler_deletes_nothing():
    """Scheduler-triggered purge with 0 deletions must not write a log entry (noise reduction)."""
    audit_service.purge_old_records(retention_days=90, triggered_by="scheduler")

    with get_session() as session:
        entry = session.query(AuditLogModel).filter_by(action="audit_purge").first()
    assert entry is None


def test_purge_preserves_parent_records_referenced_by_child():
    """A record referenced as parent_audit_id by a newer row must not be deleted."""
    old_id = _insert_record(days_ago=200)

    # Create a recent child row pointing at the old parent
    with get_session() as session:
        child = AuditLogModel(
            timestamp=datetime.now(timezone.utc) - timedelta(days=1),
            user="testuser",
            action="child_action",
            resource="test",
            status="success",
            details={},
            parent_audit_id=old_id,
        )
        session.add(child)

    deleted = audit_service.purge_old_records(retention_days=90, triggered_by="test")
    assert deleted == 0

    with get_session() as session:
        assert session.query(AuditLogModel).filter_by(id=old_id).first() is not None


def test_purge_respects_custom_retention_days():
    _insert_record(days_ago=10)
    _insert_record(days_ago=3)

    deleted = audit_service.purge_old_records(retention_days=7, triggered_by="test")
    assert deleted == 1


# ── POST /api/v1/audit/purge API endpoint ────────────────────────────────────

def test_super_admin_can_trigger_purge(super_admin_client):
    _insert_record(days_ago=100)
    resp = super_admin_client.post("/api/v1/audit/purge")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["deleted"] == 1


def test_purge_endpoint_uses_default_retention(super_admin_client):
    _insert_record(days_ago=5)
    resp = super_admin_client.post("/api/v1/audit/purge")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] == 0


def test_purge_endpoint_accepts_custom_retention_days(super_admin_client):
    _insert_record(days_ago=10)
    resp = super_admin_client.post("/api/v1/audit/purge?retention_days=7")
    assert resp.status_code == 200
    assert resp.json()["data"]["deleted"] == 1
    assert resp.json()["data"]["retention_days"] == 7


def test_observer_cannot_trigger_purge(observer_client):
    resp = observer_client.post("/api/v1/audit/purge")
    assert resp.status_code == 403


def test_operator_cannot_trigger_purge(operator_client):
    resp = operator_client.post("/api/v1/audit/purge")
    assert resp.status_code == 403


def test_unauthenticated_cannot_trigger_purge(unauth_client):
    resp = unauth_client.post("/api/v1/audit/purge")
    assert resp.status_code == 401


def test_purge_endpoint_returns_retention_days_used(super_admin_client):
    resp = super_admin_client.post("/api/v1/audit/purge?retention_days=30")
    assert resp.json()["data"]["retention_days"] == 30


# ── Config ────────────────────────────────────────────────────────────────────

def test_audit_retention_days_config_default():
    from app.core.config import AUDIT_RETENTION_DAYS
    assert AUDIT_RETENTION_DAYS == 90


def test_audit_retention_days_config_env_override(monkeypatch):
    import importlib
    import app.core.config as cfg
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "30")
    importlib.reload(cfg)
    assert cfg.AUDIT_RETENTION_DAYS == 30
    monkeypatch.delenv("AUDIT_RETENTION_DAYS", raising=False)
    importlib.reload(cfg)


def test_audit_retention_days_invalid_raises(monkeypatch):
    import importlib
    import app.core.config as cfg
    monkeypatch.setenv("AUDIT_RETENTION_DAYS", "bad")
    with pytest.raises(RuntimeError, match="AUDIT_RETENTION_DAYS"):
        importlib.reload(cfg)
    monkeypatch.delenv("AUDIT_RETENTION_DAYS", raising=False)
    importlib.reload(cfg)
