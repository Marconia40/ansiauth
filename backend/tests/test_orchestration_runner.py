"""Direct tests for ``orchestration_runner.run_operation`` — exercises the
shared lifecycle without going through any of the per-op wrappers.

The runner threads several side effects (job updates, audit events, device
locks, rate-limit waits) — those are stubbed via monkeypatch so each test can
assert just on the control-flow outcome it cares about. The goal is coverage
of the runner contract itself; the per-op wrappers already have their own
integration tests under tests/test_vlans.py / tests/test_ports_*.py.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import orchestration_runner
from app.services.orchestration_runner import (
    NoopOutcome,
    ValidationFailure,
    run_operation,
)


class _Spy:
    """Collect every side-effecting call into named lists for inspection."""

    def __init__(self) -> None:
        self.job_updates: list[tuple] = []
        self.audit_events: list[tuple] = []
        self.rollbacks: list[dict] = []
        self.executes: int = 0
        self.execute_with_state: list[dict] = []
        self.verifies: list[tuple] = []


@pytest.fixture
def spy(monkeypatch):
    """Patch every collaborator the runner reaches for and return the spy."""
    s = _Spy()

    # job_service is reached via two paths: the runner imports it at module
    # top and ``_execute_with_retry`` (lazy-imported from
    # vlan_execution_service) calls it for retry bookkeeping. Patch both
    # attribute paths so we observe every update.
    def _update_job(job_id, status=None, **kwargs):
        s.job_updates.append((job_id, status, kwargs))

    def _ensure_final_state(job_id):
        s.job_updates.append((job_id, "_ensure_final_state", {}))

    def _get_job(_job_id):
        # Never cancelled; mirror the most common path. Tests that want the
        # cancelled branch override this fixture with their own patch.
        return SimpleNamespace(status="running")

    from app.services import job_service, audit_service
    monkeypatch.setattr(job_service, "update_job", _update_job)
    monkeypatch.setattr(job_service, "ensure_final_state", _ensure_final_state)
    monkeypatch.setattr(job_service, "get_job", _get_job)

    # audit
    def _append_audit_event(audit_id, status, payload):
        s.audit_events.append((audit_id, status, payload))

    monkeypatch.setattr(audit_service, "append_audit_event", _append_audit_event)

    # device_locks + rate_limiter — both reached lazily inside run_operation
    from app.services import device_locks, rate_limiter
    from contextlib import contextmanager

    @contextmanager
    def _acquire(_device, **_kwargs):
        yield None

    monkeypatch.setattr(device_locks, "acquire", _acquire)
    monkeypatch.setattr(rate_limiter, "wait_for_slot", lambda _device: None)

    # Notification helper (reached lazily from vlan_execution_service)
    from app.services import vlan_execution_service
    monkeypatch.setattr(vlan_execution_service, "_notify_group_job_complete",
                        lambda *_a, **_kw: None)

    return s


# ── Happy path ──────────────────────────────────────────────────────────────

def test_happy_path_completes_with_pre_state_phase(spy):
    run_operation(
        job_id="J1",
        device="dev1",
        audit_id="A1",
        operation_label="test-op",
        execute=lambda: {"rc": 0, "stdout": "ok", "stderr": ""},
        capture_pre_state=lambda: {"existed": True, "data": 42},
    )
    statuses = [s for (_jid, s, _kw) in spy.audit_events]
    assert statuses == ["completed"]
    payload = spy.audit_events[0][2]
    assert payload["retries"] == 0
    assert payload["pre_state"] == {"existed": True, "data": 42}
    # Pre-state phase ops always emit rollback_performed=False on success —
    # matches the historical inline implementations that always paired
    # pre-state capture with a rollback hook.
    assert payload["rollback_performed"] is False


def test_save_style_no_pre_state_phase_omits_pre_state(spy):
    run_operation(
        job_id="JSAVE",
        device="dev1",
        audit_id="ASAVE",
        operation_label="save",
        execute=lambda: {"rc": 0, "stdout": "ok", "stderr": ""},
    )
    payload = spy.audit_events[0][2]
    assert "pre_state" not in payload
    assert "rollback_performed" not in payload


# ── Pre-state validation failure short-circuit ──────────────────────────────

def test_validation_failure_short_circuits_with_failed_audit(spy):
    def _validate(_ps):
        return ValidationFailure(
            error_msg="bad state",
            audit_extra={
                "validation": "failed",
                "reason": "demo",
                "error": {"type": "validation_error", "message": "bad state"},
            },
        )

    run_operation(
        job_id="JV",
        device="dev1",
        audit_id="AV",
        operation_label="test-op",
        execute=lambda: pytest.fail("execute should not run after a validation failure"),
        capture_pre_state=lambda: {"existed": True},
        validate_pre_state=_validate,
    )
    statuses = [s for (_jid, s, _kw) in spy.audit_events]
    assert statuses == ["failed"]
    payload = spy.audit_events[0][2]
    assert payload["validation"] == "failed"
    assert payload["reason"] == "demo"
    assert payload["pre_state"] == {"existed": True}


def test_precheck_validation_records_retry_count_zero(spy):
    def _validate(_ps):
        return ValidationFailure(
            error_msg="cannot determine state",
            audit_extra={
                "error": {"type": "precheck_failed", "message": "cannot determine state"},
                "error_type": "permanent",
            },
            is_precheck=True,
        )

    run_operation(
        job_id="JP",
        device="dev1",
        audit_id="AP",
        operation_label="test-op",
        execute=lambda: pytest.fail("execute should not run"),
        capture_pre_state=lambda: {"existed": None},
        validate_pre_state=_validate,
    )
    # The precheck branch sets retry_count=0 and rollback_performed=False on
    # the job row.
    failed_update = next(
        kw for (jid, status, kw) in spy.job_updates if status == "failed" and jid == "JP"
    )
    assert failed_update["retry_count"] == 0
    assert failed_update["rollback_performed"] is False


# ── No-op short-circuit ─────────────────────────────────────────────────────

def test_noop_short_circuits_with_completed_audit(spy):
    def _noop(_ps):
        return NoopOutcome(
            output="already in desired state",
            message="nothing to do",
            audit_reason="demo_no_op",
        )

    run_operation(
        job_id="JN",
        device="dev1",
        audit_id="AN",
        operation_label="test-op",
        execute=lambda: pytest.fail("execute should not run on a no-op"),
        capture_pre_state=lambda: {"existed": True},
        check_noop=_noop,
    )
    statuses = [s for (_jid, s, _kw) in spy.audit_events]
    assert statuses == ["completed"]
    payload = spy.audit_events[0][2]
    assert payload["reason"] == "demo_no_op"
    # The completed job_update must include the no-op output / message.
    completed_update = next(
        kw for (_jid, status, kw) in spy.job_updates if status == "completed"
    )
    assert completed_update["result"]["operation_result"] == "noop"
    assert completed_update["result"]["message"] == "nothing to do"


# ── Execute failure triggers rollback ───────────────────────────────────────

def test_execute_failure_triggers_rollback(spy):
    def _rollback(ps):
        spy.rollbacks.append(ps)
        return (True, True)

    run_operation(
        job_id="JF",
        device="dev1",
        audit_id="AF",
        operation_label="test-op",
        execute=lambda: {"rc": 1, "stdout": "", "stderr": "boom"},
        capture_pre_state=lambda: {"existed": True, "data": 99},
        rollback=_rollback,
    )
    assert spy.rollbacks == [{"existed": True, "data": 99}]
    statuses = [s for (_jid, s, _kw) in spy.audit_events]
    assert statuses == ["failed"]
    payload = spy.audit_events[0][2]
    assert payload["rollback_performed"] is True
    assert payload["rollback_success"] is True


# ── verify_after_execute can fail an otherwise-successful execute ───────────

def test_verify_after_execute_failure_triggers_rollback(spy):
    def _verify(_ps, _result):
        return "verify says no"

    def _rollback(_ps):
        return (True, False)

    run_operation(
        job_id="JVE",
        device="dev1",
        audit_id="AVE",
        operation_label="test-op",
        execute=lambda: {"rc": 0, "stdout": "ok", "stderr": ""},
        capture_pre_state=lambda: {"existed": True},
        verify_after_execute=_verify,
        rollback=_rollback,
    )
    statuses = [s for (_jid, s, _kw) in spy.audit_events]
    assert statuses == ["failed"]


# ── Unexpected exception in the body is funnelled into a failed audit ──────

def test_unexpected_exception_in_execute_is_caught(spy):
    def _explode():
        raise RuntimeError("kaboom")

    run_operation(
        job_id="JE",
        device="dev1",
        audit_id="AE",
        operation_label="test-op",
        execute=_explode,
    )
    statuses = [s for (_jid, s, _kw) in spy.audit_events]
    assert statuses == ["failed"]
    payload = spy.audit_events[0][2]
    # _execute_with_retry catches the exception and reports rc=1 with the
    # exception message as stderr, so the runner treats it as a normal
    # execute failure, not an "unexpected_error" — that's the intended
    # protocol because the lambda's exception is squarely "expected"
    # execution-time turbulence.
    assert payload["error"]["type"] == "ansible_error"


# ── Cancellation short-circuit ──────────────────────────────────────────────

def test_cancellation_before_execution_emits_cancelled_audit(monkeypatch, spy):
    # Override get_job to report cancellation on the first call.
    from app.services import job_service
    monkeypatch.setattr(
        job_service, "get_job",
        lambda _jid: SimpleNamespace(status="cancelled"),
    )

    run_operation(
        job_id="JC",
        device="dev1",
        audit_id="AC",
        operation_label="test-op",
        execute=lambda: pytest.fail("execute should not run when cancelled"),
        capture_pre_state=lambda: pytest.fail("capture should not run when cancelled"),
    )
    statuses = [s for (_jid, s, _kw) in spy.audit_events]
    assert statuses == ["cancelled"]


# ── execute_with_pre_state hook ─────────────────────────────────────────────

def test_execute_with_pre_state_receives_captured_state(spy):
    received: dict = {}

    def _execute(ps):
        received.update(ps)
        return {"rc": 0, "stdout": "ok", "stderr": ""}

    run_operation(
        job_id="JES",
        device="dev1",
        audit_id="AES",
        operation_label="test-op",
        execute_with_pre_state=_execute,
        capture_pre_state=lambda: {"mode": "trunk", "extra": 7},
    )
    assert received == {"mode": "trunk", "extra": 7}


# ── extra_audit callable is invoked once per audit emit ─────────────────────

def test_extra_audit_callable_runs_after_validate(spy):
    state = {"computed": None}

    def _validate(_ps):
        state["computed"] = [10, 20, 30]
        return None

    def _extras():
        return {"computed_vlans": state["computed"]}

    run_operation(
        job_id="JX",
        device="dev1",
        audit_id="AX",
        operation_label="test-op",
        execute=lambda: {"rc": 0, "stdout": "ok", "stderr": ""},
        capture_pre_state=lambda: {"existed": True},
        validate_pre_state=_validate,
        extra_audit=_extras,
    )
    payload = spy.audit_events[0][2]
    assert payload["computed_vlans"] == [10, 20, 30]
