"""Tests for retry backoff mechanics.

Covers: exponential delays, 5s cap, log messages, and permanent-fail
short-circuit -- ported from the deleted ``vlan_execution_service.py``
(``_execute_with_retry``) directly against its verbatim successor,
``Orquestador._ejecutar_con_retry()`` (absorbed unchanged: same
classification patterns/priority, same exponential backoff formula --
1s/2s/4s/5s-cap). This is the ONLY test file covering this timing logic in
the whole suite -- test_error_classification.py only covers classification,
not timing -- so every scenario below is ported, not dropped.

``_ejecutar_con_retry(fn, job, device, max_retries=3, retry_base_delay=1.0)``
needs a real ``Job()`` instance (it calls ``job.registrar_reintento()``
internally, not the deleted ``job_service.update_job``). Log messages are
now in Spanish under logger ``app.services.orquestador`` -- "reintentando"
before a scheduled retry sleep, "agotado" when retries are exhausted, "no
reintenta" when a permanent error short-circuits with no retry at all.
"""
import logging

import app.services.orquestador as orquestador_module
from app.composition import orquestador
from app.models.job import Job


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_fn(rc_sequence, transient_text="ssh timeout"):
    """Return a callable that returns consecutive results from rc_sequence."""
    it = iter(rc_sequence)
    def fn():
        rc = next(it)
        return {"rc": rc, "stdout": transient_text if rc != 0 else "ok", "stderr": ""}
    return fn


def _job() -> Job:
    return Job(max_retries=10)  # generous cap -- max_retries passed explicitly per call anyway


# ── delay cap ────────────────────────────────────────────────────────────────

def test_delay_capped_at_max(monkeypatch):
    """Computed delay must never exceed _MAX_RETRY_DELAY (5.0s)."""
    delays_recorded = []
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: delays_recorded.append(d))

    # Large base delay would produce 10s, 20s, 40s — all must be capped to 5s
    fn = _make_fn([1, 1, 1, 1])  # 4 failures (1 initial + 3 retries)
    orquestador._ejecutar_con_retry(fn, _job(), "cap-test-device", max_retries=3, retry_base_delay=10.0)

    assert all(d <= 5.0 for d in delays_recorded), f"Uncapped delays: {delays_recorded}"
    assert len(delays_recorded) == 3  # 3 sleeps for 3 retries


def test_normal_backoff_sequence(monkeypatch):
    """With base_delay=1 and max_retries=3 the delays must be 1s, 2s, 4s."""
    delays_recorded = []
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: delays_recorded.append(d))

    fn = _make_fn([1, 1, 1, 1])
    orquestador._ejecutar_con_retry(fn, _job(), "backoff-test-device", max_retries=3, retry_base_delay=1.0)

    assert delays_recorded == [1.0, 2.0, 4.0]


def test_backoff_capped_at_5s_for_later_attempts(monkeypatch):
    """With base_delay=1 and max_retries=5 the 4th+ delays are capped at 5s."""
    delays_recorded = []
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: delays_recorded.append(d))

    fn = _make_fn([1, 1, 1, 1, 1, 1])
    orquestador._ejecutar_con_retry(fn, _job(), "cap5-test-device", max_retries=5, retry_base_delay=1.0)

    # delays: 1s, 2s, 4s, 5s (cap, was 8), 5s (cap, was 16)
    assert delays_recorded == [1.0, 2.0, 4.0, 5.0, 5.0]


# ── retry count ───────────────────────────────────────────────────────────────

def test_retry_count_returned_correctly(monkeypatch):
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: None)

    fn = _make_fn([1, 1, 1, 0])  # succeeds on 4th attempt (3 retries)
    _, retry_count = orquestador._ejecutar_con_retry(fn, _job(), "rc-test-device", max_retries=3, retry_base_delay=0.0)
    assert retry_count == 3


def test_zero_retries_on_immediate_success(monkeypatch):
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: None)

    fn = _make_fn([0])
    _, retry_count = orquestador._ejecutar_con_retry(fn, _job(), "ok-test-device", max_retries=3, retry_base_delay=0.0)
    assert retry_count == 0


# ── permanent short-circuit ───────────────────────────────────────────────────

def test_permanent_error_no_sleep(monkeypatch):
    """Permanent errors must not sleep at all."""
    sleep_called = {"n": 0}
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: sleep_called.__setitem__("n", sleep_called["n"] + 1))

    fn = lambda: {"rc": 1, "stdout": "", "stderr": "syntax error"}
    orquestador._ejecutar_con_retry(fn, _job(), "perm-test-device", max_retries=3, retry_base_delay=1.0)

    assert sleep_called["n"] == 0


def test_permanent_error_called_once(monkeypatch):
    """Permanent errors must not result in any retry call."""
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: None)

    calls = {"n": 0}
    def fn():
        calls["n"] += 1
        return {"rc": 1, "stdout": "", "stderr": "permission denied"}

    orquestador._ejecutar_con_retry(fn, _job(), "perm2-test-device", max_retries=3, retry_base_delay=1.0)
    assert calls["n"] == 1


# ── log messages ─────────────────────────────────────────────────────────────

def test_retry_log_format(monkeypatch, caplog):
    """Log must announce each scheduled retry with the computed delay, in
    Spanish ("reintentando"/"delay=Xs"), under logger app.services.orquestador."""
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: None)

    fn = _make_fn([1, 1, 0])  # 2 retries then success
    with caplog.at_level(logging.INFO, logger="app.services.orquestador"):
        orquestador._ejecutar_con_retry(fn, _job(), "log-test-device", max_retries=3, retry_base_delay=1.0)

    retry_lines = [r.message for r in caplog.records if "reintentando" in r.message]
    assert len(retry_lines) == 2
    assert "attempt=1/4" in retry_lines[0]
    assert "attempt=2/4" in retry_lines[1]
    assert "delay=1.00s" in retry_lines[0]
    assert "delay=2.00s" in retry_lines[1]


def test_exhausted_retries_log(monkeypatch, caplog):
    """After exhausting all retries a WARNING with 'agotado' must be emitted."""
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: None)

    fn = _make_fn([1, 1, 1, 1])  # always fails transient
    with caplog.at_level(logging.WARNING, logger="app.services.orquestador"):
        orquestador._ejecutar_con_retry(fn, _job(), "exhaust-test-device", max_retries=3, retry_base_delay=0.0)

    assert any("agotado" in r.message for r in caplog.records)


def test_no_exhausted_log_on_permanent(monkeypatch, caplog):
    """'agotado' must NOT appear when the error is permanent -- it short-
    circuits via the separate 'no reintenta' warning instead."""
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: None)

    fn = lambda: {"rc": 1, "stdout": "", "stderr": "authentication failure"}
    with caplog.at_level(logging.WARNING, logger="app.services.orquestador"):
        orquestador._ejecutar_con_retry(fn, _job(), "perm-log-test-device", max_retries=3, retry_base_delay=0.0)

    assert not any("agotado" in r.message for r in caplog.records)
    assert any("no reintenta" in r.message for r in caplog.records)


def test_no_exhausted_log_on_success(monkeypatch, caplog):
    """'agotado' must NOT appear when execution eventually succeeds."""
    monkeypatch.setattr(orquestador_module.time, "sleep", lambda d: None)

    fn = _make_fn([1, 1, 0])
    with caplog.at_level(logging.WARNING, logger="app.services.orquestador"):
        orquestador._ejecutar_con_retry(fn, _job(), "success-log-test-device", max_retries=3, retry_base_delay=0.0)

    assert not any("agotado" in r.message for r in caplog.records)
