"""Tests for retry backoff mechanics (step 3.2).

Covers: exponential delays, 5s cap, log messages, and permanent-fail
short-circuit — all via the internal _execute_with_retry helper so we
can inspect exact timing and log output without going through HTTP.
"""
import logging

import pytest

import app.services.vlan_execution_service as svc


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_fn(rc_sequence):
    """Return a callable that returns consecutive results from rc_sequence."""
    it = iter(rc_sequence)
    def fn():
        rc = next(it)
        return {"rc": rc, "stdout": "ssh timeout" if rc != 0 else "ok", "stderr": ""}
    return fn


def _make_permanent_fn():
    call_count = {"n": 0}
    def fn():
        call_count["n"] += 1
        return {"rc": 1, "stdout": "", "stderr": "syntax error at line 3"}
    fn.call_count = call_count
    return fn


# ── delay cap ────────────────────────────────────────────────────────────────

def test_delay_capped_at_max(monkeypatch):
    """Computed delay must never exceed _MAX_RETRY_DELAY."""
    delays_recorded = []

    def fake_sleep(d):
        delays_recorded.append(d)

    monkeypatch.setattr(svc.time, "sleep", fake_sleep)
    monkeypatch.setattr(svc, "_MAX_RETRY_DELAY", 5.0)

    def fake_update(*a, **kw):
        pass

    monkeypatch.setattr(svc.job_service, "update_job", fake_update)

    # Large base delay would produce 10s, 20s, 40s — all must be capped to 5s
    fn = _make_fn([1, 1, 1, 1])  # 4 failures (1 initial + 3 retries)
    svc._execute_with_retry(fn, job_id="cap-test", max_retries=3, retry_base_delay=10.0)

    assert all(d <= 5.0 for d in delays_recorded), f"Uncapped delays: {delays_recorded}"
    assert len(delays_recorded) == 3  # 3 sleeps for 3 retries


def test_normal_backoff_sequence(monkeypatch):
    """With base_delay=1 and max_retries=3 the delays must be 1s, 2s, 4s."""
    delays_recorded = []

    monkeypatch.setattr(svc.time, "sleep", lambda d: delays_recorded.append(d))
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    fn = _make_fn([1, 1, 1, 1])
    svc._execute_with_retry(fn, job_id="backoff-test", max_retries=3, retry_base_delay=1.0)

    assert delays_recorded == [1.0, 2.0, 4.0]


def test_backoff_capped_at_5s_for_later_attempts(monkeypatch):
    """With base_delay=1 and max_retries=5 the 4th+ delays are capped at 5s."""
    delays_recorded = []

    monkeypatch.setattr(svc.time, "sleep", lambda d: delays_recorded.append(d))
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    fn = _make_fn([1, 1, 1, 1, 1, 1])
    svc._execute_with_retry(fn, job_id="cap5-test", max_retries=5, retry_base_delay=1.0)

    # delays: 1s, 2s, 4s, 5s (cap, was 8), 5s (cap, was 16)
    assert delays_recorded == [1.0, 2.0, 4.0, 5.0, 5.0]


# ── retry count ───────────────────────────────────────────────────────────────

def test_retry_count_returned_correctly(monkeypatch):
    monkeypatch.setattr(svc.time, "sleep", lambda d: None)
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    fn = _make_fn([1, 1, 1, 0])  # succeeds on 4th attempt (3 retries)
    _, retry_count = svc._execute_with_retry(fn, "rc-test", max_retries=3, retry_base_delay=0.0)
    assert retry_count == 3


def test_zero_retries_on_immediate_success(monkeypatch):
    monkeypatch.setattr(svc.time, "sleep", lambda d: None)
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    fn = _make_fn([0])
    _, retry_count = svc._execute_with_retry(fn, "ok-test", max_retries=3, retry_base_delay=0.0)
    assert retry_count == 0


# ── permanent short-circuit ───────────────────────────────────────────────────

def test_permanent_error_no_sleep(monkeypatch):
    """Permanent errors must not sleep at all."""
    sleep_called = {"n": 0}
    monkeypatch.setattr(svc.time, "sleep", lambda d: sleep_called.__setitem__("n", sleep_called["n"] + 1))
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    fn = lambda: {"rc": 1, "stdout": "", "stderr": "syntax error"}
    svc._execute_with_retry(fn, "perm-test", max_retries=3, retry_base_delay=1.0)

    assert sleep_called["n"] == 0


def test_permanent_error_called_once(monkeypatch):
    """Permanent errors must not result in any retry call."""
    monkeypatch.setattr(svc.time, "sleep", lambda d: None)
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        return {"rc": 1, "stdout": "", "stderr": "permission denied"}

    svc._execute_with_retry(fn, "perm2-test", max_retries=3, retry_base_delay=1.0)
    assert calls["n"] == 1


# ── log messages ─────────────────────────────────────────────────────────────

def test_retry_log_format(monkeypatch, caplog):
    """Log must say 'Retrying job X (attempt N/M) in Ys'."""
    monkeypatch.setattr(svc.time, "sleep", lambda d: None)
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    fn = _make_fn([1, 1, 0])  # 2 retries then success
    with caplog.at_level(logging.INFO, logger="app.services.vlan_execution_service"):
        svc._execute_with_retry(fn, "log-test", max_retries=3, retry_base_delay=1.0)

    retry_lines = [r.message for r in caplog.records if "retry attempt" in r.message]
    assert len(retry_lines) == 2
    assert "attempt 1/3" in retry_lines[0]
    assert "attempt 2/3" in retry_lines[1]
    assert "waiting 1s" in retry_lines[0]
    assert "waiting 2s" in retry_lines[1]


def test_exhausted_retries_log(monkeypatch, caplog):
    """After exhausting all retries a WARNING 'exhausted retries' must be emitted."""
    monkeypatch.setattr(svc.time, "sleep", lambda d: None)
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    fn = _make_fn([1, 1, 1, 1])  # always fails transient
    with caplog.at_level(logging.WARNING, logger="app.services.vlan_execution_service"):
        svc._execute_with_retry(fn, "exhaust-test", max_retries=3, retry_base_delay=0.0)

    assert any("exhausted retries" in r.message for r in caplog.records)


def test_no_exhausted_log_on_permanent(monkeypatch, caplog):
    """'exhausted retries' must NOT appear when the error is permanent."""
    monkeypatch.setattr(svc.time, "sleep", lambda d: None)
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    fn = lambda: {"rc": 1, "stdout": "", "stderr": "authentication failure"}
    with caplog.at_level(logging.WARNING, logger="app.services.vlan_execution_service"):
        svc._execute_with_retry(fn, "perm-log-test", max_retries=3, retry_base_delay=0.0)

    assert not any("exhausted retries" in r.message for r in caplog.records)


def test_no_exhausted_log_on_success(monkeypatch, caplog):
    """'exhausted retries' must NOT appear when execution eventually succeeds."""
    monkeypatch.setattr(svc.time, "sleep", lambda d: None)
    monkeypatch.setattr(svc.job_service, "update_job", lambda *a, **kw: None)

    fn = _make_fn([1, 1, 0])
    with caplog.at_level(logging.WARNING, logger="app.services.vlan_execution_service"):
        svc._execute_with_retry(fn, "success-log-test", max_retries=3, retry_base_delay=0.0)

    assert not any("exhausted retries" in r.message for r in caplog.records)
