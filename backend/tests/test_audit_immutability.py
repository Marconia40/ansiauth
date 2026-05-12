"""TEST-003 — Audit immutability regression suite.

Uses a SQLAlchemy before_execute event listener to intercept any UPDATE
statement targeting audit_logs before it reaches the database. This is
implementation-agnostic — it catches mutations from the ORM, raw SQL, or
bulk-update calls, complementing the DB-level BEFORE UPDATE trigger that
is also installed at startup.
"""
import pytest
from sqlalchemy import event

from app.db.models import AuditLogModel
from app.db.session import get_engine, get_session
from app.services import audit_service


@pytest.fixture(autouse=True)
def clear_audit():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


@pytest.fixture
def update_interceptor():
    """
    Attach a before_execute listener that raises AssertionError on any UPDATE
    against audit_logs. Yields a state dict so tests can verify the flag.
    Removed cleanly after each test regardless of outcome.
    """
    engine = get_engine()
    captured = {"updates": []}

    def _intercept(conn, clauseelement, multiparams, params, execution_options, *args, **kwargs):
        stmt = str(clauseelement)
        if "UPDATE" in stmt.upper() and "audit_logs" in stmt.lower():
            captured["updates"].append(stmt)
            raise AssertionError(f"Illegal UPDATE on audit_logs: {stmt[:200]}")

    event.listen(engine, "before_execute", _intercept)
    yield captured
    event.remove(engine, "before_execute", _intercept)


# ── log_action only inserts ────────────────────────────────────────────────────

def test_log_action_issues_no_update(update_interceptor):
    """audit_service.log_action() must only INSERT, never UPDATE."""
    audit_service.log_action("system", "test_event", "resource", {"key": "value"})
    assert update_interceptor["updates"] == []


# ── append_audit_event only inserts ──────────────────────────────────────────

def test_append_audit_event_issues_no_update(update_interceptor):
    """append_audit_event() must INSERT a new row rather than UPDATE the parent."""
    record = audit_service.log_action("system", "create_vlan", "vlan", {"vlan_id": 99})
    audit_service.append_audit_event(record.id, "completed", {"duration": 0.1})
    assert update_interceptor["updates"] == []


# ── Full lifecycle simulation ─────────────────────────────────────────────────

def test_vlan_lifecycle_produces_no_updates(update_interceptor):
    """A complete pending → completed lifecycle must issue zero UPDATE statements."""
    initial = audit_service.log_action(
        "operator", "create_vlan", "vlan",
        {"vlan_id": 200, "name": "IMMUTE_TEST"},
        status="pending",
    )
    audit_service.append_audit_event(initial.id, "completed", {"duration_seconds": 0.5})
    assert update_interceptor["updates"] == []


def test_failed_vlan_lifecycle_produces_no_updates(update_interceptor):
    """A pending → failed lifecycle must issue zero UPDATE statements."""
    initial = audit_service.log_action(
        "operator", "create_vlan", "vlan",
        {"vlan_id": 201, "name": "FAIL_TEST"},
        status="pending",
    )
    audit_service.append_audit_event(initial.id, "failed", {"error": "device unreachable"})
    assert update_interceptor["updates"] == []


# ── Status transitions create new rows ───────────────────────────────────────

def test_status_transition_inserts_new_row():
    """append_audit_event must increase the total row count by exactly one."""
    record = audit_service.log_action("system", "test_action", "resource", {}, status="pending")

    with get_session() as session:
        count_before = session.query(AuditLogModel).count()

    audit_service.append_audit_event(record.id, "completed", {})

    with get_session() as session:
        count_after = session.query(AuditLogModel).count()

    assert count_after == count_before + 1


def test_original_row_status_unchanged_after_append():
    """The initial row must retain its original status after a follow-up event is appended."""
    initial = audit_service.log_action("system", "test_op", "resource", {}, status="pending")
    audit_service.append_audit_event(initial.id, "completed", {})

    with get_session() as session:
        original = session.query(AuditLogModel).filter_by(id=int(initial.id)).first()
        assert original.status == "pending"


# ── Event chain integrity (parent_audit_id) ───────────────────────────────────

def test_initial_row_has_no_parent():
    """The first row for an operation must have parent_audit_id = None."""
    record = audit_service.log_action("operator", "create_vlan", "vlan", {})

    with get_session() as session:
        row = session.query(AuditLogModel).filter_by(id=int(record.id)).first()
        parent_id = row.parent_audit_id

    assert parent_id is None


def test_follow_up_row_parent_audit_id_links_to_initial():
    """The follow-up row's parent_audit_id must equal the initial row's integer id."""
    initial = audit_service.log_action("operator", "create_vlan", "vlan", {"vlan_id": 300})
    follow_up = audit_service.append_audit_event(initial.id, "completed", {})

    assert follow_up is not None
    assert follow_up.parent_audit_id == initial.id


def test_multi_step_chain_links_correctly():
    """A pending → running → completed chain must form a correct linked list."""
    step1 = audit_service.log_action("operator", "create_vlan", "vlan", {"vlan_id": 301}, status="pending")
    step2 = audit_service.append_audit_event(step1.id, "running", {})
    step3 = audit_service.append_audit_event(step2.id, "completed", {})

    assert step2.parent_audit_id == step1.id
    assert step3.parent_audit_id == step2.id


def test_all_statuses_present_in_chain():
    """After a full three-step lifecycle the DB must contain all three status values."""
    step1 = audit_service.log_action("operator", "create_vlan", "vlan", {"vlan_id": 302}, status="pending")
    step2 = audit_service.append_audit_event(step1.id, "running", {})
    audit_service.append_audit_event(step2.id, "completed", {})

    with get_session() as session:
        rows = session.query(AuditLogModel).filter(
            AuditLogModel.action == "create_vlan",
            AuditLogModel.resource == "vlan",
        ).all()
        statuses = {r.status for r in rows}

    assert {"pending", "running", "completed"}.issubset(statuses)


# ── Direct mutation is intercepted ───────────────────────────────────────────

def test_direct_mutation_caught_by_interceptor(update_interceptor):
    """Attempting to UPDATE audit_logs via ORM is caught by the before_execute listener."""
    record = audit_service.log_action("admin", "immutability_test", "audit_log", {})

    with pytest.raises(AssertionError, match="Illegal UPDATE on audit_logs"):
        with get_session() as session:
            row = session.query(AuditLogModel).filter_by(id=int(record.id)).first()
            row.status = "tampered"


# ── Edge cases ────────────────────────────────────────────────────────────────

def test_append_audit_event_on_nonexistent_parent_returns_none():
    """append_audit_event with an unknown parent_audit_id must return None gracefully."""
    result = audit_service.append_audit_event("99999", "completed", {})
    assert result is None
