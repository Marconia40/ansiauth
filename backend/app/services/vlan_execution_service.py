import logging
import time
import uuid

from app.core.exceptions import DeviceExecutionError
from app.services import audit_service, group_job_service, job_service, vlan_service
from app.services.retry_policy import RetryDecision, classify_error as _classify_error_string
from app.worker import celery_app

logger = logging.getLogger(__name__)

# Ansible exits with rc=4 when hosts are unreachable, rc=6 when unreachable+failed.
# rc=255 covers ansible-runner connection errors. These are always transient at the
# Ansible level regardless of the error text.
_TRANSIENT_RC_CODES = frozenset({4, 6, 255})

# Hard cap so a mis-tuned retry_base_delay never locks a device for too long.
_MAX_RETRY_DELAY: float = 5.0


def _classify_result(result: dict) -> RetryDecision:
    """Classify an Ansible result dict.

    rc-code check takes priority; string classification is delegated to
    retry_policy so both paths share the same keyword tables.
    """
    if result.get("rc") in _TRANSIENT_RC_CODES:
        return RetryDecision(
            should_retry=True,
            classification="transient",
            reason=f"ansible rc={result.get('rc')}",
        )
    return _classify_error_string(_combined_error(result))


def classify_error(result: dict) -> str:
    """Return 'transient' or 'permanent' for an Ansible result dict.

    Kept for backward compat — used by _structured_error and audit events.
    """
    return _classify_result(result).classification


def _combined_error(result: dict) -> str:
    """Return stderr + stdout combined — Ansible puts connection errors in stdout."""
    return (result.get("stderr") or "") + " " + (result.get("stdout") or "")


def _structured_error(result: dict) -> dict:
    return {
        "type": "ansible_error",
        "rc": result.get("rc", -1),
        "stderr": result.get("stderr", ""),
        "error_type": classify_error(result),
    }


def _capture_pre_state_vlan(vlan_id: int, device: str) -> dict:
    """Return VLAN pre-state for idempotency checks and rollback."""
    try:
        vlans = vlan_service.get_vlans(device)
        match = next((v for v in vlans if v.vlan_id == vlan_id), None)
        # Convert to plain dict so pre_state is JSON-serializable for DB storage
        return {"existed": match is not None, "vlan_data": match.to_dict() if match else None}
    except Exception as exc:
        logger.warning("Could not capture pre-state for VLAN %s on %s: %s", vlan_id, device, exc)
        return {"existed": None, "vlan_data": None}


def _get_pre_state(vlan_id: int, device: str) -> dict:
    """Dispatch pre-state capture through app.api.vlans so tests can monkeypatch it there."""
    import app.api.vlans as _vlans_api  # lazy: avoids circular import at module load time
    return _vlans_api._capture_pre_state_vlan(vlan_id, device)


def _execute_with_retry(fn, job_id: str, max_retries: int = 3, retry_base_delay: float = 1.0, device: str = "") -> tuple[dict, int]:
    """Call fn() up to max_retries+1 times, backing off on transient errors.

    Returns (result_dict, retry_count).
    """
    result: dict = {"rc": 1, "stdout": "", "stderr": ""}
    retry_count = 0
    for attempt in range(max_retries + 1):
        try:
            result = fn()
        except Exception as exc:
            result = {"rc": 1, "stdout": "", "stderr": str(exc)}
        if result["rc"] == 0:
            break
        decision = _classify_result(result)
        logger.info(
            "Job %s classified error as %s: %s",
            job_id, decision.classification, decision.reason,
        )
        if not decision.should_retry:
            break
        if attempt >= max_retries:
            logger.warning("Job %s exhausted retries", job_id)
            break
        delay = min(retry_base_delay * (2 ** attempt), _MAX_RETRY_DELAY)
        retry_count += 1
        error = _combined_error(result)
        job_service.update_job(
            job_id,
            retry_count=retry_count,
            last_error=error.strip()[:500],
            current_step="retrying",
        )
        logger.info(
            "Job %s device %s retry attempt %d/%d — waiting %ds",
            job_id, device, retry_count, max_retries, int(delay),
        )
        time.sleep(delay)
    return result, retry_count


def _rollback_create(vlan_id: int, device: str, job_id: str, pre_state: dict) -> tuple[bool, bool | None]:
    """Rollback a failed create operation by deleting the VLAN.

    Returns (rollback_performed, rollback_success).
    rollback_performed=False + rollback_success=None → no rollback triggered (VLAN existed before).
    rollback_performed=True + rollback_success=False → rollback attempted but failed.
    rollback_performed=True + rollback_success=True  → rollback confirmed via state check.
    """
    if pre_state.get("existed") is not False:
        logger.info(
            "Job %s rollback skipped — VLAN %s existed before operation on device=%s",
            job_id, vlan_id, device,
        )
        return False, None

    logger.info("Job %s rollback started for VLAN %s on device=%s", job_id, vlan_id, device)
    job_service.update_job(job_id, current_step="rollback_started")
    _rb_t0 = time.time()

    try:
        rb = vlan_service.delete_vlan(vlan_id, device)
    except Exception as exc:
        _rb_ms = round((time.time() - _rb_t0) * 1000)
        logger.error(
            "Job %s rollback exception for VLAN %s on device=%s in %dms: %s",
            job_id, vlan_id, device, _rb_ms, exc,
        )
        logger.warning("Job %s rollback verification failed for VLAN %s on device=%s", job_id, vlan_id, device)
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    _rb_ms = round((time.time() - _rb_t0) * 1000)
    rc = rb.get("rc", 1) if isinstance(rb, dict) else 1
    if rc != 0:
        logger.info("Job %s rollback executed for VLAN %s on device=%s in %dms", job_id, vlan_id, device, _rb_ms)
        logger.warning("Job %s rollback verification failed for VLAN %s on device=%s", job_id, vlan_id, device)
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    logger.info("Job %s rollback executed for VLAN %s on device=%s in %dms", job_id, vlan_id, device, _rb_ms)

    # rc=0: verify the VLAN is actually gone
    try:
        post_vlans = vlan_service.get_vlans(device)
        success = not any(v.vlan_id == vlan_id for v in post_vlans)
    except Exception as exc:
        logger.warning(
            "Job %s rollback state check failed for VLAN %s on device=%s: %s",
            job_id, vlan_id, device, exc,
        )
        success = False

    if success:
        logger.info("Job %s rollback verification succeeded for VLAN %s on device=%s", job_id, vlan_id, device)
    else:
        logger.warning("Job %s rollback verification failed for VLAN %s on device=%s", job_id, vlan_id, device)
    job_service.update_job(job_id, current_step="rollback_completed")
    return True, success


def _rollback_delete(vlan_id: int, device: str, job_id: str, pre_state: dict) -> tuple[bool, bool | None]:
    """Rollback a failed delete operation by recreating the VLAN.

    Returns (rollback_performed, rollback_success).
    rollback_performed=False + rollback_success=None → no rollback triggered (VLAN never existed).
    rollback_performed=True + rollback_success=False → rollback attempted but failed.
    rollback_performed=True + rollback_success=True  → rollback confirmed via state check.
    """
    if pre_state.get("existed") is not True:
        logger.info(
            "Job %s rollback skipped — VLAN %s did not exist before operation on device=%s",
            job_id, vlan_id, device,
        )
        return False, None

    vlan_data = pre_state.get("vlan_data") or {}
    original_name = vlan_data.get("name", "")
    logger.info("Job %s rollback started for VLAN %s on device=%s", job_id, vlan_id, device)
    job_service.update_job(job_id, current_step="rollback_started")
    _rb_t0 = time.time()

    try:
        rb = vlan_service.create_vlan_on_device(vlan_id, original_name, device)
    except Exception as exc:
        _rb_ms = round((time.time() - _rb_t0) * 1000)
        logger.error(
            "Job %s rollback exception for VLAN %s on device=%s in %dms: %s",
            job_id, vlan_id, device, _rb_ms, exc,
        )
        logger.warning("Job %s rollback verification failed for VLAN %s on device=%s", job_id, vlan_id, device)
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    _rb_ms = round((time.time() - _rb_t0) * 1000)
    rc = rb.get("rc", 1) if isinstance(rb, dict) else 1
    if rc != 0:
        logger.info("Job %s rollback executed for VLAN %s on device=%s in %dms", job_id, vlan_id, device, _rb_ms)
        logger.warning("Job %s rollback verification failed for VLAN %s on device=%s", job_id, vlan_id, device)
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    logger.info("Job %s rollback executed for VLAN %s on device=%s in %dms", job_id, vlan_id, device, _rb_ms)

    # rc=0: verify the VLAN is actually present again
    try:
        post_vlans = vlan_service.get_vlans(device)
        success = any(v.vlan_id == vlan_id for v in post_vlans)
    except Exception as exc:
        logger.warning(
            "Job %s rollback state check failed for VLAN %s on device=%s: %s",
            job_id, vlan_id, device, exc,
        )
        success = False

    if success:
        logger.info("Job %s rollback verification succeeded for VLAN %s on device=%s", job_id, vlan_id, device)
    else:
        logger.warning("Job %s rollback verification failed for VLAN %s on device=%s", job_id, vlan_id, device)
    job_service.update_job(job_id, current_step="rollback_completed")
    return True, success


def _rollback_update(vlan_id: int, device: str, job_id: str, pre_state: dict) -> tuple[bool, bool | None]:
    """Rollback a failed update by restoring the previous VLAN name.

    Returns (rollback_performed, rollback_success).
    rollback_performed=False + rollback_success=None → no rollback triggered (no previous name).
    rollback_performed=True + rollback_success=False → rollback attempted but failed.
    rollback_performed=True + rollback_success=True  → rollback confirmed via state check.
    """
    prev_name = (pre_state.get("vlan_data") or {}).get("name")
    if prev_name is None:
        logger.info(
            "Job %s rollback skipped — no previous name in pre-state for VLAN %s on device=%s",
            job_id, vlan_id, device,
        )
        return False, None

    logger.info("Job %s rollback started for VLAN %s on device=%s", job_id, vlan_id, device)
    job_service.update_job(job_id, current_step="rollback_started")
    _rb_t0 = time.time()

    try:
        rb = vlan_service.update_vlan_description(vlan_id, prev_name, device)
    except Exception as exc:
        _rb_ms = round((time.time() - _rb_t0) * 1000)
        logger.error(
            "Job %s rollback exception for VLAN %s on device=%s in %dms: %s",
            job_id, vlan_id, device, _rb_ms, exc,
        )
        logger.warning("Job %s rollback verification failed for VLAN %s on device=%s", job_id, vlan_id, device)
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    _rb_ms = round((time.time() - _rb_t0) * 1000)
    rc = rb.get("rc", 1) if isinstance(rb, dict) else 1
    if rc != 0:
        logger.info(
            "Job %s rollback executed for VLAN %s on device=%s in %dms (name='%s')",
            job_id, vlan_id, device, _rb_ms, prev_name,
        )
        logger.warning("Job %s rollback verification failed for VLAN %s on device=%s", job_id, vlan_id, device)
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    logger.info(
        "Job %s rollback executed for VLAN %s on device=%s in %dms (name='%s')",
        job_id, vlan_id, device, _rb_ms, prev_name,
    )

    # rc=0: verify the name was actually restored
    try:
        post_vlans = vlan_service.get_vlans(device)
        match = next((v for v in post_vlans if v.vlan_id == vlan_id), None)
        success = bool(match and match.name.lower() == prev_name.lower())
    except Exception as exc:
        logger.warning(
            "Job %s rollback state check failed for VLAN %s on device=%s: %s",
            job_id, vlan_id, device, exc,
        )
        success = False

    if success:
        logger.info("Job %s rollback verification succeeded for VLAN %s on device=%s", job_id, vlan_id, device)
    else:
        logger.warning("Job %s rollback verification failed for VLAN %s on device=%s", job_id, vlan_id, device)
    job_service.update_job(job_id, current_step="rollback_completed")
    return True, success


def _notify_group_job_complete(group_job_id: str, job_id: str, device: str) -> None:
    """Push the final per-device job state into the parent GroupJob and re-aggregate its status."""
    try:
        job = job_service.get_job(job_id)
        if not job:
            return
        duration_ms = None
        if job.started_at and job.finished_at:
            from datetime import timezone as _tz
            started = job.started_at if job.started_at.tzinfo else job.started_at.replace(tzinfo=_tz.utc)
            finished = job.finished_at if job.finished_at.tzinfo else job.finished_at.replace(tzinfo=_tz.utc)
            duration_ms = round((finished - started).total_seconds() * 1000)
        group_job_service.update_device_result(
            group_job_id=group_job_id,
            device=device,
            job_id=job_id,
            status=job.status,
            current_step=job.current_step,
            retry_count=job.retry_count,
            rollback_performed=job.rollback_performed,
            rollback_success=job.rollback_success,
            error=job.error,
            duration_ms=duration_ms,
        )
    except Exception as exc:
        logger.warning("Could not update group job %s for device=%s: %s", group_job_id, device, exc)


def run_create_job(job_id: str, vlan_id: int, name: str, device: str, audit_id: str, retry_base_delay: float = 1.0, pre_state: dict | None = None, group_job_id: str | None = None):
    from app.services.orchestration_runner import NoopOutcome, ValidationFailure, run_operation

    def _validate(ps: dict) -> ValidationFailure | None:
        if ps.get("existed") is None:
            msg = f"Cannot determine VLAN state on device '{device}' — aborting operation"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "error": {"type": "precheck_failed", "message": msg},
                    "error_type": "permanent",
                },
                is_precheck=True,
            )
        if ps.get("existed") is True:
            existing_name = (ps.get("vlan_data") or {}).get("name", "")
            if existing_name.lower() != name.lower():
                msg = "VLAN already exists with a different name"
                return ValidationFailure(
                    error_msg=msg,
                    audit_extra={
                        "validation": "failed",
                        "reason": "vlan_already_exists",
                        "error": {"type": "validation_error", "message": msg},
                    },
                )
        return None

    def _noop(ps: dict) -> NoopOutcome | None:
        if ps.get("existed") is True:
            existing_name = (ps.get("vlan_data") or {}).get("name", "")
            if existing_name.lower() == name.lower():
                return NoopOutcome(
                    output=f"VLAN {vlan_id} already configured, no changes needed",
                    message="VLAN already exists (no changes needed)",
                    audit_reason="vlan_already_exists_no_op",
                )
        return None

    def _success_log(duration: float, retry_count: int) -> None:
        logger.info(
            "Job %s: completed — VLAN %s created on device=%s in %.2fs (retries=%d)",
            job_id, vlan_id, device, duration, retry_count,
        )

    run_operation(
        job_id=job_id,
        device=device,
        audit_id=audit_id,
        operation_label=f"create VLAN {vlan_id}",
        execute=lambda: vlan_service.create_vlan_on_device(vlan_id, name, device),
        retry_base_delay=retry_base_delay,
        pre_state=pre_state,
        group_job_id=group_job_id,
        capture_pre_state=lambda: _get_pre_state(vlan_id, device),
        validate_pre_state=_validate,
        check_noop=_noop,
        rollback=lambda ps: _rollback_create(vlan_id, device, job_id, ps),
        logger=logger,
        success_log=_success_log,
    )


def run_delete_job(job_id: str, vlan_id: int, device: str, audit_id: str, retry_base_delay: float = 1.0, pre_state: dict | None = None, group_job_id: str | None = None):
    """Delete one VLAN on one device.

    Two behaviours specific to delete force this wrapper to be a touch more
    elaborate than the create/update wrappers:

    1. A pre-execute existence re-check (separate from the pre-state capture)
       — if the VLAN is already gone we fail loudly so the user knows. The
       check is wrapped so a lookup failure logs a warning and proceeds rather
       than aborting.
    2. A post-execute verification that the VLAN is actually absent — caught
       via the runner's ``verify_after_execute`` hook.
    """
    from app.services.orchestration_runner import ValidationFailure, run_operation

    # Shared scratch box so the wrapped execute() callable can communicate a
    # pre-execute existence error back to the runner without raising (which
    # would route through the unexpected-error branch).
    _scratch: dict = {"pre_exec_error": None}

    def _validate(ps: dict) -> ValidationFailure | None:
        if ps.get("existed") is None:
            msg = f"Cannot determine VLAN state on device '{device}' — aborting operation"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "error": {"type": "precheck_failed", "message": msg},
                    "error_type": "permanent",
                },
                is_precheck=True,
            )
        return None

    def _execute() -> dict:
        # Pre-execute existence re-check — historical behaviour: a lookup
        # failure logs a warning and we still run the delete; an absent VLAN
        # is a hard failure that funnels through the rollback path.
        try:
            existing = vlan_service.get_vlans(device)
            if not any(v.vlan_id == vlan_id for v in existing):
                return {
                    "rc": 1,
                    "stdout": "",
                    "stderr": f"VLAN {vlan_id} does not exist on device '{device}'",
                }
        except Exception as exc:
            logger.warning("Job %s: could not verify VLAN pre-existence: %s", job_id, exc)
        return vlan_service.delete_vlan(vlan_id, device)

    def _verify(_ps: dict, result: dict) -> str | None:
        try:
            remaining = vlan_service.get_vlans(device)
        except Exception as exc:
            logger.warning("Job %s: could not verify VLAN deletion: %s", job_id, exc)
            return None
        if any(v.vlan_id == vlan_id for v in remaining):
            logger.error("Job %s: VLAN %s still present after deletion on %s", job_id, vlan_id, device)
            return "VLAN still present after deletion"
        return None

    def _success_log(duration: float, retry_count: int) -> None:
        logger.info(
            "Job %s: completed — VLAN %s deleted on device=%s in %.2fs (retries=%d)",
            job_id, vlan_id, device, duration, retry_count,
        )

    run_operation(
        job_id=job_id,
        device=device,
        audit_id=audit_id,
        operation_label=f"delete VLAN {vlan_id}",
        execute=_execute,
        retry_base_delay=retry_base_delay,
        pre_state=pre_state,
        group_job_id=group_job_id,
        capture_pre_state=lambda: _get_pre_state(vlan_id, device),
        validate_pre_state=_validate,
        rollback=lambda ps: _rollback_delete(vlan_id, device, job_id, ps),
        verify_after_execute=_verify,
        logger=logger,
        success_log=_success_log,
    )


def run_update_job(job_id: str, vlan_id: int, description: str, device: str, audit_id: str, retry_base_delay: float = 1.0, pre_state: dict | None = None, group_job_id: str | None = None):
    from app.services.orchestration_runner import NoopOutcome, ValidationFailure, run_operation

    def _validate(ps: dict) -> ValidationFailure | None:
        if ps.get("existed") is None:
            msg = f"Cannot determine VLAN state on device '{device}' — aborting operation"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "error": {"type": "precheck_failed", "message": msg},
                    "error_type": "permanent",
                },
                is_precheck=True,
            )
        if ps.get("existed") is False:
            msg = f"VLAN {vlan_id} does not exist on device '{device}'"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "validation": "failed",
                    "reason": "vlan_not_found",
                    "error": {"type": "validation_error", "message": msg},
                },
            )
        return None

    def _noop(ps: dict) -> NoopOutcome | None:
        existing_name = (ps.get("vlan_data") or {}).get("name", "")
        if existing_name and existing_name.lower() == description.lower():
            return NoopOutcome(
                output=f"VLAN {vlan_id} already has this name, no changes needed",
                message="VLAN already has requested name (no changes needed)",
                audit_reason="vlan_name_unchanged_no_op",
            )
        return None

    def _success_log(duration: float, retry_count: int) -> None:
        logger.info(
            "Job %s: completed — VLAN %s updated on device=%s in %.2fs (retries=%d)",
            job_id, vlan_id, device, duration, retry_count,
        )

    run_operation(
        job_id=job_id,
        device=device,
        audit_id=audit_id,
        operation_label=f"update VLAN {vlan_id}",
        execute=lambda: vlan_service.update_vlan_description(vlan_id, description, device),
        retry_base_delay=retry_base_delay,
        pre_state=pre_state,
        group_job_id=group_job_id,
        capture_pre_state=lambda: _get_pre_state(vlan_id, device),
        validate_pre_state=_validate,
        check_noop=_noop,
        rollback=lambda ps: _rollback_update(vlan_id, device, job_id, ps),
        logger=logger,
        success_log=_success_log,
    )


def run_save_job(job_id: str, device: str, audit_id: str, retry_base_delay: float = 1.0, group_job_id: str | None = None):
    """Save running-config on one device.

    Save has no pre-state phase and no rollback (it's idempotent and we can't
    really undo a config-write). The wrapper passes only ``execute`` to the
    runner; pre_state-related audit keys are omitted automatically.
    """
    from app.services.orchestration_runner import run_operation

    def _success_log(duration: float, _retry_count: int) -> None:
        logger.info("Job %s: completed — config saved on device=%s in %.2fs", job_id, device, duration)

    run_operation(
        job_id=job_id,
        device=device,
        audit_id=audit_id,
        operation_label="save config",
        execute=lambda: vlan_service.save_config_on_device(device),
        retry_base_delay=retry_base_delay,
        group_job_id=group_job_id,
        logger=logger,
        success_log=_success_log,
    )


# ── Sequential group runners ──────────────────────────────────────────────────
#
# Each group runner executes one background task per group job. Devices run
# strictly one after the other. A device failure is caught inside run_*_job
# (which never propagates exceptions) so the loop always continues to the
# next device — providing fault isolation across the fleet.
#
# device_tasks tuples carry only the data needed to call the corresponding
# run_*_job; retry_base_delay and group_job_id are passed as separate args.

def _log_group_outcome(group_job_id: str, operation: str) -> None:
    """Fetch and log the final aggregate status after all devices have run."""
    try:
        gj = group_job_service.get_group_job(group_job_id)
        if gj:
            s = gj.execution_summary()
            logger.info(
                "GroupJob %s: %s complete — status=%s completed=%d/%d",
                group_job_id, operation, gj.status, s["completed"], s["total_devices"],
            )
    except Exception as exc:
        logger.warning("GroupJob %s: could not fetch final status: %s", group_job_id, exc)


def run_group_create_job(
    group_job_id: str,
    device_tasks: list[tuple],  # (job_id, vlan_id, name, device, audit_id, pre_state)
    retry_base_delay: float = 1.0,
) -> None:
    logger.info("GroupJob %s: sequential create starting — %d device(s)", group_job_id, len(device_tasks))
    for job_id, vlan_id, name, device, audit_id, pre_state in device_tasks:
        logger.info("GroupJob %s: executing device=%s", group_job_id, device)
        run_create_job(job_id, vlan_id, name, device, audit_id, retry_base_delay,
                       pre_state=pre_state, group_job_id=group_job_id)
        logger.info("GroupJob %s: finished device=%s", group_job_id, device)
    _log_group_outcome(group_job_id, "create")


def run_group_delete_job(
    group_job_id: str,
    device_tasks: list[tuple],  # (job_id, vlan_id, device, audit_id, pre_state)
    retry_base_delay: float = 1.0,
) -> None:
    logger.info("GroupJob %s: sequential delete starting — %d device(s)", group_job_id, len(device_tasks))
    for job_id, vlan_id, device, audit_id, pre_state in device_tasks:
        logger.info("GroupJob %s: executing device=%s", group_job_id, device)
        run_delete_job(job_id, vlan_id, device, audit_id, retry_base_delay,
                       pre_state=pre_state, group_job_id=group_job_id)
        logger.info("GroupJob %s: finished device=%s", group_job_id, device)
    _log_group_outcome(group_job_id, "delete")


def run_group_update_job(
    group_job_id: str,
    device_tasks: list[tuple],  # (job_id, vlan_id, description, device, audit_id, pre_state)
    retry_base_delay: float = 1.0,
) -> None:
    logger.info("GroupJob %s: sequential update starting — %d device(s)", group_job_id, len(device_tasks))
    for job_id, vlan_id, description, device, audit_id, pre_state in device_tasks:
        logger.info("GroupJob %s: executing device=%s", group_job_id, device)
        run_update_job(job_id, vlan_id, description, device, audit_id, retry_base_delay,
                       pre_state=pre_state, group_job_id=group_job_id)
        logger.info("GroupJob %s: finished device=%s", group_job_id, device)
    _log_group_outcome(group_job_id, "update")


# ── Celery task wrappers (dispatch gate) ──────────────────────────────────────

@celery_app.task(name="ansiauth.vlan.group_create")
def _group_create_task(group_job_id: str, device_tasks: list, retry_base_delay: float) -> None:
    run_group_create_job(group_job_id, device_tasks, retry_base_delay)


@celery_app.task(name="ansiauth.vlan.group_delete")
def _group_delete_task(group_job_id: str, device_tasks: list, retry_base_delay: float) -> None:
    run_group_delete_job(group_job_id, device_tasks, retry_base_delay)


@celery_app.task(name="ansiauth.vlan.group_update")
def _group_update_task(group_job_id: str, device_tasks: list, retry_base_delay: float) -> None:
    run_group_update_job(group_job_id, device_tasks, retry_base_delay)


@celery_app.task(name="ansiauth.vlan.save")
def _save_task(job_id: str, device: str, audit_id: str, retry_base_delay: float, group_job_id: str | None) -> None:
    run_save_job(job_id, device, audit_id, retry_base_delay, group_job_id)


# ── Enqueue helpers (called by route handlers) ────────────────────────────────

def enqueue_create_jobs(vlan, username: str, retry_base_delay: float) -> tuple[list[dict], str]:
    from app.services import device_locks
    request_id = str(uuid.uuid4())
    group_job = group_job_service.create_group_job(
        operation="create_vlan",
        playbook="create_vlan.yml",
        parameters={"vlan_id": vlan.vlan_id, "name": vlan.name},
        devices=list(vlan.devices),
    )
    device_tasks = []
    job_entries = []
    for dev_name in vlan.devices:
        job = job_service.create_job(
            playbook="create_vlan.yml",
            device=dev_name,
            parameters={"vlan_id": vlan.vlan_id, "name": vlan.name},
            group_job_id=group_job.group_job_id,
        )
        audit = audit_service.log_action(
            user=username, action="create_vlan", resource="vlan",
            details={"vlan_id": vlan.vlan_id, "name": vlan.name},
            status="pending", job_id=job.job_id, device=dev_name, request_id=request_id,
        )
        try:
            with device_locks.acquire(dev_name, timeout=30):
                pre_state = _get_pre_state(vlan.vlan_id, dev_name)
        except TimeoutError:
            logger.warning("Device %s busy during pre-state capture for VLAN %s — job will abort on start", dev_name, vlan.vlan_id)
            pre_state = {"existed": None, "vlan_data": None}
        device_tasks.append((job.job_id, vlan.vlan_id, vlan.name, dev_name, audit.id, pre_state))
        job_entries.append({"device": dev_name, "job_id": job.job_id, "status": job.status})
    _group_create_task.delay(group_job.group_job_id, device_tasks, retry_base_delay)
    return job_entries, group_job.group_job_id


def enqueue_delete_jobs(vlan_id: int, devices: list[str], username: str, retry_base_delay: float) -> tuple[list[dict], str]:
    from app.services import device_locks
    request_id = str(uuid.uuid4())
    group_job = group_job_service.create_group_job(
        operation="delete_vlan",
        playbook="delete_vlan.yml",
        parameters={"vlan_id": vlan_id},
        devices=devices,
    )
    device_tasks = []
    job_entries = []
    for dev_name in devices:
        job = job_service.create_job(
            playbook="delete_vlan.yml",
            device=dev_name,
            parameters={"vlan_id": vlan_id},
            group_job_id=group_job.group_job_id,
        )
        audit = audit_service.log_action(
            user=username, action="delete_vlan", resource="vlan",
            details={"vlan_id": vlan_id, "device": dev_name},
            status="pending", job_id=job.job_id, device=dev_name, request_id=request_id,
        )
        try:
            with device_locks.acquire(dev_name, timeout=30):
                pre_state = _get_pre_state(vlan_id, dev_name)
        except TimeoutError:
            logger.warning("Device %s busy during pre-state capture for VLAN %s — job will abort on start", dev_name, vlan_id)
            pre_state = {"existed": None, "vlan_data": None}
        device_tasks.append((job.job_id, vlan_id, dev_name, audit.id, pre_state))
        job_entries.append({"device": dev_name, "job_id": job.job_id, "status": job.status})
    _group_delete_task.delay(group_job.group_job_id, device_tasks, retry_base_delay)
    return job_entries, group_job.group_job_id


def enqueue_save_job(device_name: str, username: str, retry_base_delay: float = 1.0) -> dict:
    group_job = group_job_service.create_group_job(
        operation="save_config",
        playbook="save_config.yml",
        parameters={},
        devices=[device_name],
    )
    job = job_service.create_job(
        playbook="save_config.yml",
        device=device_name,
        parameters={},
        group_job_id=group_job.group_job_id,
    )
    audit = audit_service.log_action(
        user=username, action="save_config", resource="device",
        details={"device": device_name},
        status="pending", job_id=job.job_id, device=device_name,
    )
    _save_task.delay(job.job_id, device_name, audit.id, retry_base_delay, group_job.group_job_id)
    return {"device": device_name, "job_id": job.job_id, "status": job.status, "group_job_id": group_job.group_job_id}


def enqueue_update_jobs(vlan_id: int, data, username: str, retry_base_delay: float) -> tuple[list[dict], str]:
    from app.services import device_locks
    request_id = str(uuid.uuid4())
    group_job = group_job_service.create_group_job(
        operation="update_vlan",
        playbook="update_vlan.yml",
        parameters={"vlan_id": vlan_id, "description": data.description},
        devices=list(data.devices),
    )
    device_tasks = []
    job_entries = []
    for dev_name in data.devices:
        job = job_service.create_job(
            playbook="update_vlan.yml",
            device=dev_name,
            parameters={"vlan_id": vlan_id, "description": data.description},
            group_job_id=group_job.group_job_id,
        )
        audit = audit_service.log_action(
            user=username, action="update_vlan", resource="vlan",
            details={"vlan_id": vlan_id, "description": data.description, "device": dev_name},
            status="pending", job_id=job.job_id, device=dev_name, request_id=request_id,
        )
        try:
            with device_locks.acquire(dev_name, timeout=30):
                pre_state = _get_pre_state(vlan_id, dev_name)
        except TimeoutError:
            logger.warning("Device %s busy during pre-state capture for VLAN %s — job will abort on start", dev_name, vlan_id)
            pre_state = {"existed": None, "vlan_data": None}
        device_tasks.append((job.job_id, vlan_id, data.description, dev_name, audit.id, pre_state))
        job_entries.append({"device": dev_name, "job_id": job.job_id, "status": job.status})
    _group_update_task.delay(group_job.group_job_id, device_tasks, retry_base_delay)
    return job_entries, group_job.group_job_id
