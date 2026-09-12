"""TEST-003 — Audit immutability regression suite.

Two complementary defences are exercised here:

* A SQLAlchemy ``before_execute`` listener (installed per-test by the
  ``update_interceptor`` fixture) catches any UPDATE statement targeting
  ``audit_logs`` before it reaches the database. Useful for asserting that
  high-level service calls don't issue updates.
* The application-layer ``audit_guard.AuditImmutabilityError`` raised by the
  ``before_flush`` listener installed in :func:`app.db.session.init_db` —
  the portable replacement for the old SQLite-only BEFORE UPDATE trigger.
  This guard fires earlier (at flush time) so direct ORM mutations never
  even reach the ``before_execute`` interceptor.

``audit_service.log_action()``/``append_audit_event()`` (the free functions
this file used to exercise) are gone -- ``AuditRepository.append()`` is the
sole write path now, and there is no direct replacement for the old
"follow-up row" helper (``append_audit_event``). The two local helpers below
(``_log``/``_append_followup``) rebuild that same shape by hand (as
instructed): a plain ``AuditRecord`` insert, and a second one carrying
``parent_audit_id`` + merged ``details``. This is legitimate test-only
scaffolding to exercise the real, still-live guarantees this file is about
(insert-only writes, the immutability guard, parent_audit_id chaining as a
raw AuditRepository capability) -- it does NOT claim any production code
path still builds multi-step pending/running/completed chains itself (it
doesn't, see test_audit.py's module docstring for the live-verified
Orquestador behavior).
"""
import pytest
from sqlalchemy import event

from app.composition import audit_repository
from app.db.audit_guard import AuditImmutabilityError
from app.db.models import AuditLogModel
from app.db.session import get_engine, get_session
from app.models.audit import AuditRecord


def _clear_audit_log() -> None:
    with get_session() as session:
        session.query(AuditLogModel).delete(synchronize_session=False)


def _log(user: str, action: str, resource: str, details: dict, *, status: str = "success", device=None) -> AuditRecord:
    return audit_repository.append(AuditRecord(
        user=user, action=action, resource=resource, details=details,
        status=status, device=device,
    ))


def _append_followup(parent: AuditRecord, status: str, extra_details: dict) -> AuditRecord:
    """Test-only stand-in for the deleted ``audit_service.append_audit_event()``
    -- builds a new row linked to *parent* via ``parent_audit_id``, merging
    ``extra_details`` on top of the parent's own ``details`` (same contract
    the old free function had)."""
    return audit_repository.append(AuditRecord(
        parent_audit_id=parent.id,
        user=parent.user, action=parent.action, resource=parent.resource,
        resource_id=parent.resource_id, device=parent.device,
        details={**parent.details, **extra_details},
        status=status,
    ))


@pytest.fixture(autouse=True)
def clear_audit():
    _clear_audit_log()
    yield
    _clear_audit_log()


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
    """A plain AuditRepository.append() must only INSERT, never UPDATE."""
    _log("system", "test_event", "resource", {"key": "value"})
    assert update_interceptor["updates"] == []


# ── append_audit_event only inserts ──────────────────────────────────────────

def test_append_audit_event_issues_no_update(update_interceptor):
    """A follow-up append() must INSERT a new row rather than UPDATE the parent."""
    record = _log("system", "create_vlan", "vlan", {"vlan_id": 99})
    _append_followup(record, "completed", {"duration": 0.1})
    assert update_interceptor["updates"] == []


# ── Full lifecycle simulation ─────────────────────────────────────────────────

def test_vlan_lifecycle_produces_no_updates(update_interceptor):
    """A complete pending → completed lifecycle must issue zero UPDATE statements."""
    initial = _log(
        "operator", "create_vlan", "vlan",
        {"vlan_id": 200, "name": "IMMUTE_TEST"},
        status="pending",
    )
    _append_followup(initial, "completed", {"duration_seconds": 0.5})
    assert update_interceptor["updates"] == []


def test_failed_vlan_lifecycle_produces_no_updates(update_interceptor):
    """A pending → failed lifecycle must issue zero UPDATE statements."""
    initial = _log(
        "operator", "create_vlan", "vlan",
        {"vlan_id": 201, "name": "FAIL_TEST"},
        status="pending",
    )
    _append_followup(initial, "failed", {"error": "device unreachable"})
    assert update_interceptor["updates"] == []


# ── Status transitions create new rows ───────────────────────────────────────

def test_status_transition_inserts_new_row():
    """A follow-up append() must increase the total row count by exactly one."""
    record = _log("system", "test_action", "resource", {}, status="pending")

    with get_session() as session:
        count_before = session.query(AuditLogModel).count()

    _append_followup(record, "completed", {})

    with get_session() as session:
        count_after = session.query(AuditLogModel).count()

    assert count_after == count_before + 1


def test_original_row_status_unchanged_after_append():
    """The initial row must retain its original status after a follow-up event is appended."""
    initial = _log("system", "test_op", "resource", {}, status="pending")
    _append_followup(initial, "completed", {})

    with get_session() as session:
        original = session.query(AuditLogModel).filter_by(id=int(initial.id)).first()
        assert original.status == "pending"


# ── Event chain integrity (parent_audit_id) ───────────────────────────────────

def test_initial_row_has_no_parent():
    """The first row for an operation must have parent_audit_id = None."""
    record = _log("operator", "create_vlan", "vlan", {})

    with get_session() as session:
        row = session.query(AuditLogModel).filter_by(id=int(record.id)).first()
        parent_id = row.parent_audit_id

    assert parent_id is None


# GENUINE BUG (not an intentional architecture change) found while porting
# this file: AuditRepository.append() never copies record.parent_audit_id
# onto the AuditLogModel row it builds (app/repositories/audit_repository.py,
# the `AuditLogModel(...)` constructor call inside append() lists
# timestamp/user/action/resource/resource_id/details/summary/status/job_id/
# device/request_id but not parent_audit_id). The column itself is real
# (audit_logs.parent_audit_id, FK to audit_logs.id), _to_domain() reads it
# back out, purge_old() queries it to protect referenced parents from
# deletion, and AuditImmutabilityError's own message says "set
# parent_audit_id to chain it to this one" -- but nothing ever persists it
# on write, so every row's parent_audit_id is silently NULL regardless of
# what the caller passed in. Left failing on purpose (not weakened) to
# surface this — not fixed here per this task's test-only scope.
def test_follow_up_row_parent_audit_id_links_to_initial():
    """The follow-up row's parent_audit_id must equal the initial row's integer id."""
    initial = _log("operator", "create_vlan", "vlan", {"vlan_id": 300})
    follow_up = _append_followup(initial, "completed", {})

    assert follow_up is not None
    assert follow_up.parent_audit_id == initial.id


def test_multi_step_chain_links_correctly():
    """A pending → running → completed chain must form a correct linked list."""
    step1 = _log("operator", "create_vlan", "vlan", {"vlan_id": 301}, status="pending")
    step2 = _append_followup(step1, "running", {})
    step3 = _append_followup(step2, "completed", {})

    assert step2.parent_audit_id == step1.id
    assert step3.parent_audit_id == step2.id


def test_all_statuses_present_in_chain():
    """After a full three-step lifecycle the DB must contain all three status values."""
    step1 = _log("operator", "create_vlan", "vlan", {"vlan_id": 302}, status="pending")
    step2 = _append_followup(step1, "running", {})
    _append_followup(step2, "completed", {})

    with get_session() as session:
        rows = session.query(AuditLogModel).filter(
            AuditLogModel.action == "create_vlan",
            AuditLogModel.resource == "vlan",
        ).all()
        statuses = {r.status for r in rows}

    assert {"pending", "running", "completed"}.issubset(statuses)


# ── Direct mutation is intercepted ───────────────────────────────────────────

def test_direct_mutation_caught_by_guard():
    """Attempting to UPDATE audit_logs via ORM raises AuditImmutabilityError
    at flush time — caught by the app-layer guard installed in init_db.

    No fixture-level interceptor is needed: the guard fires before the SQL
    is generated, so the UPDATE never reaches the engine."""
    record = _log("admin", "immutability_test", "audit_log", {})

    with pytest.raises(AuditImmutabilityError, match=r"audit_logs row id=\d+ is immutable"):
        with get_session() as session:
            row = session.query(AuditLogModel).filter_by(id=int(record.id)).first()
            row.status = "tampered"


# ── Edge cases ────────────────────────────────────────────────────────────────
#
# test_append_audit_event_on_nonexistent_parent_returns_none dropped: it
# tested audit_service.append_audit_event()'s own defensive "unknown
# parent_audit_id -> return None instead of crashing" behavior. That was a
# property of the deleted free function itself, not of AuditRepository.append()
# -- there's no real production helper left with that contract to port the
# assertion against (see task's architecture note: "There is NO direct
# replacement for the old append_audit_event(...) helper").
