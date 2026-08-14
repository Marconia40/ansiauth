import logging
import time
import uuid

from fastapi import BackgroundTasks

from app.core.exceptions import DeviceExecutionError
from app.services import audit_service, job_service, vlan_service

logger = logging.getLogger(__name__)

# Ansible exits with rc=4 when hosts are unreachable, rc=6 when unreachable+failed.
# rc=255 covers ansible-runner connection errors. These are always transient.
_TRANSIENT_RC_CODES = frozenset({4, 6, 255})

# Keyword fallback for rc=2 errors that are still connection-related (e.g. timeout
# during a task, SSH refused mid-session). "unreachable" is intentionally excluded:
# Ansible's PLAY RECAP always prints "unreachable=0" even on healthy runs, which
# would cause every task failure to be misclassified as transient.
_RETRYABLE_KEYWORDS = (
    "timeout",
    "timed out",
    "connection refused",
    "unable to connect",
    "ssh failure",
    "ssh error",
    "network is unreachable",
    "no route to host",
)


def classify_error(result: dict) -> str:
    """
    Returns:
        'transient' → retryable (network/SSH issues)
        'permanent' → non-retryable (validation, logic, fail task)
    """
    if result.get("rc") in _TRANSIENT_RC_CODES:
        return "transient"
    error_text = _combined_error(result).lower()
    if any(kw in error_text for kw in _RETRYABLE_KEYWORDS):
        return "transient"
    return "permanent"


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
        match = next((v for v in vlans if v["vlan_id"] == vlan_id), None)
        return {"existed": match is not None, "vlan_data": match}
    except Exception as exc:
        logger.warning("Could not capture pre-state for VLAN %s on %s: %s", vlan_id, device, exc)
        return {"existed": None, "vlan_data": None}


def _get_pre_state(vlan_id: int, device: str) -> dict:
    """Dispatch pre-state capture through app.api.vlans so tests can monkeypatch it there."""
    import app.api.vlans as _vlans_api  # lazy: avoids circular import at module load time
    return _vlans_api._capture_pre_state_vlan(vlan_id, device)


def _execute_with_retry(fn, job_id: str, max_retries: int = 3, retry_base_delay: float = 1.0) -> tuple[dict, int]:
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
        error_type = classify_error(result)
        error_text = _combined_error(result)
        logger.info("Job %s classified error as %s: %s", job_id, error_type, error_text[:200])
        if error_type == "permanent":
            break
        if attempt >= max_retries:
            break
        delay = retry_base_delay * (2 ** attempt)
        retry_count += 1
        error = _combined_error(result)
        job_service.update_job(
            job_id,
            retry_count=retry_count,
            last_error=error.strip()[:500],
            current_step="retrying",
        )
        logger.info(
            "Job %s: transient error, retry %d/%d in %.1fs: %s",
            job_id, retry_count, max_retries, delay, error.strip(),
        )
        time.sleep(delay)
    return result, retry_count


def run_create_job(job_id: str, vlan_id: int, name: str, device: str, audit_id: str, retry_base_delay: float = 1.0):
    from app.services import device_locks, rate_limiter

    start_time = time.time()
    pre_state: dict | None = None
    logger.info("Job %s: queued — create VLAN %s on device=%s", job_id, vlan_id, device)

    try:
        _job = job_service.get_job(job_id)
        if _job and _job.status == "cancelled":
            audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
            return

        rate_limiter.wait_for_slot(device)

        lock = device_locks.get_device_lock(device)
        with lock:
            _job = job_service.get_job(job_id)
            if _job and _job.status == "cancelled":
                audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
                return

            job_service.update_job(job_id, "running")
            logger.info("Job %s: started — create VLAN %s on device=%s", job_id, vlan_id, device)

            pre_state = _get_pre_state(vlan_id, device)
            job_service.update_job(job_id, pre_state=pre_state)

            if pre_state.get("existed") is None:
                error_msg = f"Cannot determine VLAN state on device '{device}' — aborting operation"
                logger.error("Job %s: pre-state check failed on device=%s", job_id, device)
                job_service.update_job(job_id, "failed", error=error_msg, retry_count=0, rollback_performed=False)
                audit_service.append_audit_event(audit_id, "failed", {
                    "error": {"type": "precheck_failed", "message": error_msg},
                    "error_type": "permanent",
                    "pre_state": pre_state,
                })
                return

            if pre_state.get("existed") is True:
                existing_name = (pre_state.get("vlan_data") or {}).get("name", "")
                if existing_name.lower() == name.lower():
                    duration = time.time() - start_time
                    logger.info("Job %s: VLAN %s already exists on %s with same name — no-op", job_id, vlan_id, device)
                    job_service.update_job(
                        job_id, "completed",
                        result={"output": f"VLAN {vlan_id} already configured, no changes needed"},
                        current_step="completed",
                    )
                    audit_service.append_audit_event(audit_id, "completed", {
                        "reason": "vlan_already_exists_no_op",
                        "duration_seconds": round(duration, 2),
                        "pre_state": pre_state,
                    })
                    return
                else:
                    error_msg = "VLAN already exists with a different name"
                    logger.warning("Job %s: validation failed — %s on device=%s", job_id, error_msg, device)
                    job_service.update_job(job_id, "failed", error=error_msg)
                    audit_service.append_audit_event(audit_id, "failed", {
                        "validation": "failed",
                        "reason": "vlan_already_exists",
                        "error": {"type": "validation_error", "message": error_msg},
                        "pre_state": pre_state,
                    })
                    return

            result, retry_count = _execute_with_retry(
                lambda: vlan_service.create_vlan_on_device(vlan_id, name, device),
                job_id,
                retry_base_delay=retry_base_delay,
            )

            duration = time.time() - start_time
            rollback_performed = False

            if result["rc"] != 0:
                if pre_state.get("existed") is False:
                    try:
                        rb = vlan_service.delete_vlan(vlan_id, device)
                        logger.info("Rollback raw result: %s", rb)
                        if isinstance(rb, dict):
                            rollback_performed = rb.get("rc", 1) == 0
                        else:
                            rollback_performed = False
                        if not rollback_performed and isinstance(rb, dict) and rb.get("rc") == 2:
                            try:
                                post_vlans = vlan_service.get_vlans(device)
                                rollback_performed = not any(v["vlan_id"] == vlan_id for v in post_vlans)
                                logger.info(
                                    "Rollback state check: VLAN %s %s on %s",
                                    vlan_id, "absent" if rollback_performed else "still present", device,
                                )
                            except Exception:
                                pass
                        logger.warning(
                            "Job %s: rollback executed for VLAN %s — %s",
                            job_id, vlan_id, "succeeded" if rollback_performed else "failed",
                        )
                    except Exception as rb_exc:
                        logger.error("Rollback exception: %s", rb_exc)
                        rollback_performed = False
                rollback_performed = bool(rollback_performed)
                logger.info(
                    "Rollback decision — existed=%s rollback_performed=%s",
                    pre_state.get("existed"), rollback_performed,
                )

                error_msg = result.get("stderr") or result.get("stdout") or "Execution failed"
                error_output = _combined_error(result)
                logger.error(
                    "Job %s: failed — create VLAN %s on device=%s rc=%d error=%s",
                    job_id, vlan_id, device, result.get("rc", -1), error_output.strip()[:300],
                )
                job_service.update_job(
                    job_id, "failed", error=error_msg,
                    retry_count=retry_count, rollback_performed=rollback_performed,
                )
                audit_service.append_audit_event(audit_id, "failed", {
                    "retries": retry_count,
                    "rollback_performed": rollback_performed,
                    "duration_seconds": round(duration, 2),
                    "error": _structured_error(result),
                    "error_type": classify_error(result),
                    "pre_state": pre_state,
                })
            else:
                job_service.update_job(
                    job_id, "completed", result={"output": result["stdout"]},
                    retry_count=retry_count,
                )
                audit_service.append_audit_event(audit_id, "completed", {
                    "retries": retry_count,
                    "rollback_performed": False,
                    "duration_seconds": round(duration, 2),
                    "pre_state": pre_state,
                })
                logger.info(
                    "Job %s: completed — VLAN %s created on device=%s in %.2fs (retries=%d)",
                    job_id, vlan_id, device, duration, retry_count,
                )

    except Exception as exc:
        duration = time.time() - start_time
        error_msg = str(exc)
        logger.error("Job %s: unexpected failure on device=%s: %s", job_id, device, error_msg)
        job_service.update_job(job_id, "failed", error=error_msg)
        audit_service.append_audit_event(audit_id, "failed", {
            "error": {"type": "unexpected_error", "message": error_msg},
            "duration_seconds": round(duration, 2),
            "error_type": "permanent",
            "pre_state": pre_state,
        })

    finally:
        job_service.ensure_final_state(job_id)


def run_delete_job(job_id: str, vlan_id: int, device: str, audit_id: str, retry_base_delay: float = 1.0):
    from app.services import device_locks, rate_limiter

    start_time = time.time()
    pre_state: dict | None = None
    logger.info("Job %s: queued — delete VLAN %s on device=%s", job_id, vlan_id, device)

    try:
        _job = job_service.get_job(job_id)
        if _job and _job.status == "cancelled":
            audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
            return

        rate_limiter.wait_for_slot(device)

        lock = device_locks.get_device_lock(device)
        with lock:
            _job = job_service.get_job(job_id)
            if _job and _job.status == "cancelled":
                audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
                return

            job_service.update_job(job_id, "running")
            logger.info("Job %s: started — delete VLAN %s on device=%s", job_id, vlan_id, device)

            pre_state = _get_pre_state(vlan_id, device)
            job_service.update_job(job_id, pre_state=pre_state)

            if pre_state.get("existed") is None:
                error_msg = f"Cannot determine VLAN state on device '{device}' — aborting operation"
                logger.error("Job %s: pre-state check failed on device=%s", job_id, device)
                job_service.update_job(job_id, "failed", error=error_msg, retry_count=0, rollback_performed=False)
                audit_service.append_audit_event(audit_id, "failed", {
                    "error": {"type": "precheck_failed", "message": error_msg},
                    "error_type": "permanent",
                    "pre_state": pre_state,
                })
                return

            retry_count = 0
            rollback_performed = False
            success = False
            stdout = ""
            error_msg = None
            last_result: dict = {"rc": -1, "stderr": ""}

            try:
                try:
                    existing = vlan_service.get_vlans(device)
                    if not any(v["vlan_id"] == vlan_id for v in existing):
                        raise DeviceExecutionError(
                            f"VLAN {vlan_id} does not exist on device '{device}'"
                        )
                except DeviceExecutionError:
                    raise
                except Exception as exc:
                    logger.warning("Job %s: could not verify VLAN pre-existence: %s", job_id, exc)

                result, retry_count = _execute_with_retry(
                    lambda: vlan_service.delete_vlan(vlan_id, device),
                    job_id,
                    retry_base_delay=retry_base_delay,
                )
                last_result = result

                if result["rc"] != 0:
                    raise DeviceExecutionError(
                        result.get("stderr") or result.get("stdout") or "delete_vlan returned non-zero rc"
                    )

                try:
                    remaining = vlan_service.get_vlans(device)
                except Exception as exc:
                    logger.warning("Job %s: could not verify VLAN deletion: %s", job_id, exc)
                    remaining = None

                if remaining is not None and any(v["vlan_id"] == vlan_id for v in remaining):
                    logger.error("Job %s: VLAN %s still present after deletion on %s", job_id, vlan_id, device)
                    raise DeviceExecutionError("VLAN still present after deletion")

                success = True
                stdout = result.get("stdout", "")

            except Exception as exc:
                error_msg = str(exc)

            duration = time.time() - start_time

            if success:
                job_service.update_job(
                    job_id, "completed", result={"output": stdout}, retry_count=retry_count,
                )
                audit_service.append_audit_event(audit_id, "completed", {
                    "retries": retry_count,
                    "rollback_performed": False,
                    "duration_seconds": round(duration, 2),
                    "pre_state": pre_state,
                })
                logger.info(
                    "Job %s: completed — VLAN %s deleted on device=%s in %.2fs (retries=%d)",
                    job_id, vlan_id, device, duration, retry_count,
                )
            else:
                if pre_state.get("existed") is True:
                    try:
                        vlan_data = pre_state.get("vlan_data") or {}
                        rb = vlan_service.create_vlan_on_device(
                            vlan_id, vlan_data.get("name", ""), device
                        )
                        logger.info("Rollback raw result: %s", rb)
                        if isinstance(rb, dict):
                            rollback_performed = rb.get("rc", 1) == 0
                        else:
                            rollback_performed = False
                        if not rollback_performed and isinstance(rb, dict) and rb.get("rc") == 2:
                            try:
                                post_vlans = vlan_service.get_vlans(device)
                                rollback_performed = any(v["vlan_id"] == vlan_id for v in post_vlans)
                                logger.info(
                                    "Rollback state check: VLAN %s %s on %s",
                                    vlan_id, "present" if rollback_performed else "absent", device,
                                )
                            except Exception:
                                pass
                        logger.warning(
                            "Job %s: rollback executed for VLAN %s — %s",
                            job_id, vlan_id, "succeeded" if rollback_performed else "failed",
                        )
                    except Exception as rb_exc:
                        logger.error("Rollback exception: %s", rb_exc)
                        rollback_performed = False
                rollback_performed = bool(rollback_performed)
                logger.info(
                    "Rollback decision — existed=%s rollback_performed=%s",
                    pre_state.get("existed"), rollback_performed,
                )

                logger.error(
                    "Job %s: failed — delete VLAN %s on device=%s: %s",
                    job_id, vlan_id, device, error_msg,
                )
                job_service.update_job(
                    job_id, "failed", error=error_msg,
                    retry_count=retry_count, rollback_performed=rollback_performed,
                )
                audit_service.append_audit_event(audit_id, "failed", {
                    "retries": retry_count,
                    "rollback_performed": rollback_performed,
                    "duration_seconds": round(duration, 2),
                    "error": _structured_error(last_result),
                    "error_type": classify_error(last_result),
                    "pre_state": pre_state,
                })

    except Exception as exc:
        duration = time.time() - start_time
        error_msg = str(exc)
        logger.error("Job %s: unexpected failure on device=%s: %s", job_id, device, error_msg)
        job_service.update_job(job_id, "failed", error=error_msg)
        audit_service.append_audit_event(audit_id, "failed", {
            "error": {"type": "unexpected_error", "message": error_msg},
            "duration_seconds": round(duration, 2),
            "error_type": "permanent",
            "pre_state": pre_state,
        })

    finally:
        job_service.ensure_final_state(job_id)


def run_update_job(job_id: str, vlan_id: int, description: str, device: str, audit_id: str, retry_base_delay: float = 1.0):
    from app.services import device_locks, rate_limiter

    start_time = time.time()
    pre_state: dict | None = None
    logger.info("Job %s: queued — update VLAN %s on device=%s", job_id, vlan_id, device)

    try:
        _job = job_service.get_job(job_id)
        if _job and _job.status == "cancelled":
            audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
            return

        rate_limiter.wait_for_slot(device)

        lock = device_locks.get_device_lock(device)
        with lock:
            _job = job_service.get_job(job_id)
            if _job and _job.status == "cancelled":
                audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
                return

            job_service.update_job(job_id, "running")
            logger.info("Job %s: started — update VLAN %s on device=%s", job_id, vlan_id, device)

            pre_state = _get_pre_state(vlan_id, device)
            job_service.update_job(job_id, pre_state=pre_state)

            if pre_state.get("existed") is None:
                error_msg = f"Cannot determine VLAN state on device '{device}' — aborting operation"
                logger.error("Job %s: pre-state check failed on device=%s", job_id, device)
                job_service.update_job(job_id, "failed", error=error_msg, retry_count=0, rollback_performed=False)
                audit_service.append_audit_event(audit_id, "failed", {
                    "error": {"type": "precheck_failed", "message": error_msg},
                    "error_type": "permanent",
                    "pre_state": pre_state,
                })
                return

            vlan_present: bool | None = pre_state.get("existed")
            if vlan_present is False:
                error_msg = f"VLAN {vlan_id} does not exist on device '{device}'"
                logger.warning("Job %s: validation failed — %s", job_id, error_msg)
                job_service.update_job(job_id, "failed", error=error_msg)
                audit_service.append_audit_event(audit_id, "failed", {
                    "validation": "failed",
                    "reason": "vlan_not_found",
                    "error": {"type": "validation_error", "message": error_msg},
                    "pre_state": pre_state,
                })
                return

            existing_name = (pre_state.get("vlan_data") or {}).get("name", "")
            if existing_name and existing_name.lower() == description.lower():
                duration = time.time() - start_time
                logger.info("Job %s: VLAN %s on %s already named '%s' — no-op", job_id, vlan_id, device, description)
                job_service.update_job(
                    job_id, "completed",
                    result={"output": f"VLAN {vlan_id} already has this name, no changes needed"},
                    current_step="completed",
                )
                audit_service.append_audit_event(audit_id, "completed", {
                    "reason": "vlan_name_unchanged_no_op",
                    "duration_seconds": round(duration, 2),
                    "pre_state": pre_state,
                })
                return

            result, retry_count = _execute_with_retry(
                lambda: vlan_service.update_vlan_description(vlan_id, description, device),
                job_id,
                retry_base_delay=retry_base_delay,
            )

            duration = time.time() - start_time
            rollback_performed = False

            if result["rc"] != 0:
                prev_name = (pre_state.get("vlan_data") or {}).get("name")
                if prev_name is not None:
                    try:
                        rb = vlan_service.update_vlan_description(vlan_id, prev_name, device)
                        logger.info("Rollback raw result: %s", rb)
                        if isinstance(rb, dict):
                            rollback_performed = rb.get("rc", 1) == 0
                        else:
                            rollback_performed = False
                        if not rollback_performed and isinstance(rb, dict) and rb.get("rc") == 2:
                            try:
                                post_vlans = vlan_service.get_vlans(device)
                                match = next((v for v in post_vlans if v["vlan_id"] == vlan_id), None)
                                rollback_performed = bool(match and match.get("name", "").lower() == prev_name.lower())
                                logger.info(
                                    "Rollback state check: VLAN %s name=%s expected=%s",
                                    vlan_id, match.get("name") if match else None, prev_name,
                                )
                            except Exception:
                                pass
                        logger.warning(
                            "Job %s: rollback executed for VLAN %s — %s (restored name '%s')",
                            job_id, vlan_id, "succeeded" if rollback_performed else "failed", prev_name,
                        )
                    except Exception as rb_exc:
                        logger.error("Rollback exception: %s", rb_exc)
                        rollback_performed = False
                rollback_performed = bool(rollback_performed)
                logger.info(
                    "Rollback decision — existed=%s rollback_performed=%s",
                    pre_state.get("existed"), rollback_performed,
                )

                error_msg = result.get("stderr") or result.get("stdout") or "Execution failed"
                error_output = _combined_error(result)
                logger.error(
                    "Job %s: failed — update VLAN %s on device=%s: rc=%d error=%s",
                    job_id, vlan_id, device, result.get("rc", -1), error_output.strip()[:300],
                )
                job_service.update_job(
                    job_id, "failed", error=error_msg,
                    retry_count=retry_count, rollback_performed=rollback_performed,
                )
                audit_service.append_audit_event(audit_id, "failed", {
                    "retries": retry_count,
                    "rollback_performed": rollback_performed,
                    "duration_seconds": round(duration, 2),
                    "error": _structured_error(result),
                    "error_type": classify_error(result),
                    "pre_state": pre_state,
                })
            else:
                job_service.update_job(
                    job_id, "completed", result={"output": result["stdout"]},
                    retry_count=retry_count,
                )
                audit_service.append_audit_event(audit_id, "completed", {
                    "retries": retry_count,
                    "rollback_performed": False,
                    "duration_seconds": round(duration, 2),
                    "pre_state": pre_state,
                })
                logger.info(
                    "Job %s: completed — VLAN %s updated on device=%s in %.2fs (retries=%d)",
                    job_id, vlan_id, device, duration, retry_count,
                )

    except Exception as exc:
        duration = time.time() - start_time
        error_msg = str(exc)
        logger.error("Job %s: unexpected failure on device=%s: %s", job_id, device, error_msg)
        job_service.update_job(job_id, "failed", error=error_msg)
        audit_service.append_audit_event(audit_id, "failed", {
            "error": {"type": "unexpected_error", "message": error_msg},
            "duration_seconds": round(duration, 2),
            "error_type": "permanent",
            "pre_state": pre_state,
        })

    finally:
        job_service.ensure_final_state(job_id)


# ── Enqueue helpers (called by route handlers) ────────────────────────────────

def enqueue_create_jobs(vlan, username: str, background_tasks: BackgroundTasks, retry_base_delay: float) -> list[dict]:
    request_id = str(uuid.uuid4())
    job_entries = []
    for dev_name in vlan.devices:
        job = job_service.create_job(
            playbook="create_vlan.yml",
            device=dev_name,
            parameters={"vlan_id": vlan.vlan_id, "name": vlan.name},
        )
        audit = audit_service.log_action(
            user=username, action="create_vlan", resource="vlan",
            details={"vlan_id": vlan.vlan_id, "name": vlan.name},
            status="pending", job_id=job.job_id, device=dev_name, request_id=request_id,
        )
        background_tasks.add_task(
            run_create_job, job.job_id, vlan.vlan_id, vlan.name, dev_name, audit.id, retry_base_delay,
        )
        job_entries.append({"device": dev_name, "job_id": job.job_id, "status": job.status})
    return job_entries


def enqueue_delete_jobs(vlan_id: int, devices: list[str], username: str, background_tasks: BackgroundTasks, retry_base_delay: float) -> list[dict]:
    request_id = str(uuid.uuid4())
    job_entries = []
    for dev_name in devices:
        job = job_service.create_job(
            playbook="delete_vlan.yml",
            device=dev_name,
            parameters={"vlan_id": vlan_id},
        )
        audit = audit_service.log_action(
            user=username, action="delete_vlan", resource="vlan",
            details={"vlan_id": vlan_id, "device": dev_name},
            status="pending", job_id=job.job_id, device=dev_name, request_id=request_id,
        )
        background_tasks.add_task(
            run_delete_job, job.job_id, vlan_id, dev_name, audit.id, retry_base_delay,
        )
        job_entries.append({"device": dev_name, "job_id": job.job_id, "status": job.status})
    return job_entries


def enqueue_update_jobs(vlan_id: int, data, username: str, background_tasks: BackgroundTasks, retry_base_delay: float) -> list[dict]:
    request_id = str(uuid.uuid4())
    job_entries = []
    for dev_name in data.devices:
        job = job_service.create_job(
            playbook="update_vlan.yml",
            device=dev_name,
            parameters={"vlan_id": vlan_id, "description": data.description},
        )
        audit = audit_service.log_action(
            user=username, action="update_vlan", resource="vlan",
            details={"vlan_id": vlan_id, "description": data.description, "device": dev_name},
            status="pending", job_id=job.job_id, device=dev_name, request_id=request_id,
        )
        background_tasks.add_task(
            run_update_job, job.job_id, vlan_id, data.description, dev_name, audit.id, retry_base_delay,
        )
        job_entries.append({"device": dev_name, "job_id": job.job_id, "status": job.status})
    return job_entries
