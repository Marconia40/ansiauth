"""Templated runner for single-device write operations.

Before this module existed, every ``run_<op>_job`` function in
``vlan_execution_service`` and ``port_execution_service`` re-implemented the
same skeleton:

    queued log →
    pre-lock cancel check →
    rate limit →
    device lock →
    in-lock cancel check →
    mark running →
    capture pre-state (optional) →
    validate pre-state (optional) →
    no-op short-circuit (optional) →
    execute with retry →
    post-execute verify (optional) →
    on failure: rollback (optional) + audit "failed" →
    on success: audit "completed" →
    except: audit "failed" (unexpected_error) →
    finally: ensure_final_state + group-job notify

``run_operation`` collapses that skeleton into one function and lets each
operation plug in its own callables. Callers pass a ``logger`` so log records
keep their original module-of-origin — several tests use ``caplog`` keyed on
those logger names.

Audit payload shapes are preserved byte-for-byte against the inline
implementations they replace; that contract is what lets the existing test
suite pass unmodified.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.services import audit_service, job_service

# The retry helper, classifier, and group-job notify live in
# ``vlan_execution_service`` for historical reasons (and because test cases
# assert on log records emitted under that module's logger name). Importing
# them lazily inside ``run_operation`` avoids the circular import that would
# otherwise form once vlan_execution_service imports from this module.

_module_logger = logging.getLogger(__name__)


@dataclass
class ValidationFailure:
    """Returned from ``validate_pre_state`` to short-circuit with a 'failed' audit.

    ``audit_extra`` is merged into the failed-audit payload after the runner
    appends the captured ``pre_state``. Different ops emit different shapes:

    * Precheck failures (e.g. "existed is None") include
      ``{"error_type": "permanent", "error": {"type": "precheck_failed", ...}}``.
    * Validation failures (e.g. "VLAN exists with a different name") include
      ``{"validation": "failed", "reason": "...", "error": {"type": "validation_error", ...}}``.

    The runner doesn't enforce a shape — callers supply whatever keys their
    audit contract requires.
    """
    error_msg: str
    audit_extra: dict = field(default_factory=dict)
    # When True, the runner records retry_count=0 and rollback_performed=False
    # on the job row (mirrors the precheck branch in the inline implementations).
    is_precheck: bool = False


@dataclass
class NoopOutcome:
    """Returned from ``check_noop`` to short-circuit with a 'completed' no-op.

    ``output`` and ``message`` become the result body on the job row;
    ``audit_reason`` is recorded under the ``reason`` key in the audit event.
    """
    output: str
    message: str
    audit_reason: str


ExecuteFn = Callable[[], dict]
ExecuteWithPreStateFn = Callable[[dict], dict]
PreStateCaptureFn = Callable[[], dict]
PreStateValidateFn = Callable[[dict], "ValidationFailure | None"]
NoopCheckFn = Callable[[dict], "NoopOutcome | None"]
RollbackFn = Callable[[dict], tuple[bool, "bool | None"]]
VerifyFn = Callable[[dict, dict], "str | None"]
SuccessLogFn = Callable[[float, int], None]


def run_operation(
    *,
    # ── Required ──────────────────────────────────────────────────────────────
    job_id: str,
    device: str,
    audit_id: str,
    operation_label: str,
    # Exactly one of execute / execute_with_pre_state must be provided. The
    # second variant receives the captured pre_state and is used by ops that
    # need to branch playbook choice on it (e.g. access vs trunk-PVID).
    execute: ExecuteFn | None = None,
    execute_with_pre_state: ExecuteWithPreStateFn | None = None,
    # ── Job control ───────────────────────────────────────────────────────────
    retry_base_delay: float = 1.0,
    pre_state: dict | None = None,
    group_job_id: str | None = None,
    # ── Per-operation hooks (all optional) ────────────────────────────────────
    capture_pre_state: PreStateCaptureFn | None = None,
    validate_pre_state: PreStateValidateFn | None = None,
    check_noop: NoopCheckFn | None = None,
    rollback: RollbackFn | None = None,
    verify_after_execute: VerifyFn | None = None,
    # ── Audit / logging shape ────────────────────────────────────────────────
    # ``extra_audit`` is merged into both success and post-execute failure
    # audit payloads. Pass a callable when the extras depend on values that
    # the validate / no-op hooks compute (e.g. the trunk runner's desired
    # VLAN list); the callable is invoked when the payload is built.
    extra_audit: "dict | Callable[[], dict] | None" = None,
    logger: logging.Logger | None = None,
    success_log: SuccessLogFn | None = None,
) -> None:
    """Run the canonical lifecycle for one device operation.

    Every callable hook is optional. Operations that don't have a pre-state
    phase (e.g. ``save_config``) leave ``capture_pre_state`` and
    ``validate_pre_state`` as ``None`` — the runner then omits ``pre_state``
    from every audit payload it emits, matching the previous inline behavior.
    """
    from app.services import device_locks, rate_limiter
    from app.services.vlan_execution_service import (
        _combined_error,
        _execute_with_retry,
        _notify_group_job_complete,
        _structured_error,
        classify_error,
    )

    log = logger or _module_logger
    start_time = time.time()
    log.info("Job %s: queued — %s on device=%s", job_id, operation_label, device)

    has_pre_state_phase = capture_pre_state is not None or pre_state is not None

    try:
        if _is_cancelled(job_id):
            audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
            return

        rate_limiter.wait_for_slot(device)

        with device_locks.acquire(device):
            if _is_cancelled(job_id):
                audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
                return

            job_service.update_job(job_id, "running")
            log.info("Job %s: started — %s on device=%s", job_id, operation_label, device)

            # Pre-state phase
            if has_pre_state_phase:
                if pre_state is None:
                    pre_state = capture_pre_state()
                job_service.update_job(job_id, pre_state=pre_state)

                if validate_pre_state is not None:
                    failure = validate_pre_state(pre_state)
                    if failure is not None:
                        log.warning("Job %s: validation failed — %s", job_id, failure.error_msg)
                        if failure.is_precheck:
                            job_service.update_job(
                                job_id, "failed", error=failure.error_msg,
                                retry_count=0, rollback_performed=False,
                            )
                        else:
                            job_service.update_job(job_id, "failed", error=failure.error_msg)
                        audit_payload = dict(failure.audit_extra)
                        audit_payload["pre_state"] = pre_state
                        audit_service.append_audit_event(audit_id, "failed", audit_payload)
                        return

                if check_noop is not None:
                    noop = check_noop(pre_state)
                    if noop is not None:
                        duration = time.time() - start_time
                        log.info("Job %s: no-op — %s on device=%s", job_id, operation_label, device)
                        job_service.update_job(
                            job_id, "completed",
                            result={
                                "output": noop.output,
                                "operation_result": "noop",
                                "message": noop.message,
                            },
                            current_step="completed",
                        )
                        audit_service.append_audit_event(audit_id, "completed", {
                            "reason": noop.audit_reason,
                            "duration_seconds": round(duration, 2),
                            "pre_state": pre_state,
                        })
                        return

            # Execute (with retry). Two callable shapes are supported: an
            # arg-free ``execute`` (the common case) or
            # ``execute_with_pre_state(pre_state)`` for ops that need to
            # branch playbook choice on the captured state.
            if execute_with_pre_state is not None:
                _ps_for_exec = pre_state if pre_state is not None else {}
                _fn = lambda: execute_with_pre_state(_ps_for_exec)
            elif execute is not None:
                _fn = execute
            else:
                raise ValueError("run_operation requires execute or execute_with_pre_state")
            result, retry_count = _execute_with_retry(
                _fn,
                job_id,
                retry_base_delay=retry_base_delay,
                device=device,
            )

            duration = time.time() - start_time

            # Optional post-execute verification (used by destructive ops to
            # confirm the change actually took effect on the device).
            verify_error: str | None = None
            if result["rc"] == 0 and verify_after_execute is not None:
                verify_error = verify_after_execute(pre_state, result) if pre_state is not None else verify_after_execute({}, result)

            extras = extra_audit() if callable(extra_audit) else extra_audit

            if result["rc"] != 0 or verify_error is not None:
                rollback_performed: bool = False
                rollback_success: bool | None = None
                if rollback is not None and pre_state is not None:
                    rollback_performed, rollback_success = rollback(pre_state)

                error_msg = (
                    verify_error
                    or result.get("stderr") or result.get("stdout") or "Execution failed"
                )
                error_output = _combined_error(result)
                log.error(
                    "Job %s: failed — %s on device=%s: rc=%d error=%s",
                    job_id, operation_label, device, result.get("rc", -1),
                    error_output.strip()[:300],
                )
                update_kwargs: dict[str, Any] = {
                    "error": error_msg,
                    "retry_count": retry_count,
                }
                if rollback is not None:
                    update_kwargs["rollback_performed"] = rollback_performed
                    update_kwargs["rollback_success"] = rollback_success
                    update_kwargs["current_step"] = "rollback_completed" if rollback_performed else None
                job_service.update_job(job_id, "failed", **update_kwargs)

                audit_service.append_audit_event(audit_id, "failed", _ordered_failed_payload(
                    retry_count=retry_count,
                    rollback_performed=rollback_performed if rollback is not None else None,
                    rollback_success=rollback_success if rollback is not None else None,
                    duration=duration,
                    structured_error=_structured_error(result),
                    error_type=classify_error(result),
                    pre_state=pre_state,
                    has_rollback=rollback is not None,
                    extra_audit=extras,
                ))
            else:
                job_service.update_job(
                    job_id, "completed", result={"output": result["stdout"]},
                    retry_count=retry_count,
                )
                audit_service.append_audit_event(audit_id, "completed", _ordered_completed_payload(
                    retry_count=retry_count,
                    duration=duration,
                    pre_state=pre_state,
                    has_rollback=rollback is not None,
                    has_pre_state_phase=has_pre_state_phase,
                    extra_audit=extras,
                ))
                if success_log is not None:
                    success_log(duration, retry_count)
                else:
                    log.info(
                        "Job %s: completed — %s on device=%s in %.2fs (retries=%d)",
                        job_id, operation_label, device, duration, retry_count,
                    )

    except Exception as exc:
        duration = time.time() - start_time
        error_msg = str(exc)
        log.error("Job %s: unexpected failure on device=%s: %s", job_id, device, error_msg)
        job_service.update_job(job_id, "failed", error=error_msg)
        unexpected_payload: dict[str, Any] = {
            "error": {"type": "unexpected_error", "message": error_msg},
            "duration_seconds": round(duration, 2),
            "error_type": "permanent",
        }
        # Include pre_state only when a pre-state phase exists — save-style
        # ops omit it. Match the inline implementations which always include
        # the variable even when None, except for save which omits it entirely.
        if has_pre_state_phase:
            unexpected_payload["pre_state"] = pre_state
        audit_service.append_audit_event(audit_id, "failed", unexpected_payload)

    finally:
        job_service.ensure_final_state(job_id)
        if group_job_id:
            _notify_group_job_complete(group_job_id, job_id, device)


def _is_cancelled(job_id: str) -> bool:
    job = job_service.get_job(job_id)
    return job is not None and job.status == "cancelled"


def _ordered_failed_payload(
    *,
    retry_count: int,
    rollback_performed: bool | None,
    rollback_success: bool | None,
    duration: float,
    structured_error: dict,
    error_type: str,
    pre_state: dict | None,
    has_rollback: bool,
    extra_audit: dict | None,
) -> dict:
    """Build the 'failed' audit payload with keys in the historical order so
    snapshot-style assertions against the rendered JSON keep passing."""
    payload: dict[str, Any] = {"retries": retry_count}
    if has_rollback:
        payload["rollback_performed"] = rollback_performed
        payload["rollback_success"] = rollback_success
    payload["duration_seconds"] = round(duration, 2)
    payload["error"] = structured_error
    payload["error_type"] = error_type
    if pre_state is not None:
        payload["pre_state"] = pre_state
    if extra_audit:
        payload.update(extra_audit)
    return payload


def _ordered_completed_payload(
    *,
    retry_count: int,
    duration: float,
    pre_state: dict | None,
    has_rollback: bool,
    has_pre_state_phase: bool,
    extra_audit: dict | None,
) -> dict:
    """Build the 'completed' audit payload matching the historical order."""
    payload: dict[str, Any] = {"retries": retry_count}
    # Save-style runners never emit rollback_performed or pre_state on success.
    if has_pre_state_phase:
        payload["rollback_performed"] = False
    payload["duration_seconds"] = round(duration, 2)
    if pre_state is not None:
        payload["pre_state"] = pre_state
    if extra_audit:
        payload.update(extra_audit)
    return payload
