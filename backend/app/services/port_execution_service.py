"""Orchestration for port-level write operations.

Step 2.1 introduces port-description editing as the first port mutation.
This module is the equivalent of ``vlan_execution_service`` but scoped to
ports: it captures pre-state, applies retry on transient failures, and
rolls back the description on failure by re-issuing the previous value.

Design notes
------------
* Single-device per request.  The spec for step 2.1 explicitly limits the
  scope to one device (PATCH ``/api/v1/ports/description``), so there's
  no group-job runner here.  The response shape still surfaces a
  ``group_job_id`` for frontend consistency — the per-device job
  notification UX is built around that contract.
* Reuses ``vlan_execution_service`` helpers (``_classify_result``,
  ``_combined_error``, ``_execute_with_retry``,
  ``_notify_group_job_complete``) so retry semantics and group-job
  aggregation behave identically to VLAN flows.
* Rollback is "describe the previous value" — symmetric and verifiable.
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import BackgroundTasks

from app.services import (
    audit_service,
    group_job_service,
    job_service,
    port_service,
    vlan_execution_service,
)

logger = logging.getLogger(__name__)


# Re-export shared helpers from vlan_execution_service so tests and audit
# events use the same classifier / error shaping the rest of the codebase
# already trusts.
_classify_result = vlan_execution_service._classify_result
_combined_error = vlan_execution_service._combined_error
_classify_error = vlan_execution_service.classify_error
_structured_error = vlan_execution_service._structured_error
_execute_with_retry = vlan_execution_service._execute_with_retry
_notify_group_job_complete = vlan_execution_service._notify_group_job_complete


# ── Pre-state capture ────────────────────────────────────────────────────────

def _capture_pre_state_description(interface: str, device: str) -> dict:
    """Return the current description of *interface* on *device*, plus a
    presence flag so rollback knows whether the port even exists.

    The returned dict is JSON-serializable for storage on the job record.

    Keys:
        existed:        True  → port exists on the device
                        False → port not found  (rollback will skip)
                        None  → could not be determined  (rollback will skip)
        description:    prior value (None when port has no description or
                        when ``existed`` is not True)
    """
    try:
        response = port_service.list_ports(device)
        match = next((p for p in response.ports if p.name == interface), None)
        if match is None:
            return {"existed": False, "description": None}
        return {"existed": True, "description": match.description}
    except Exception as exc:
        logger.warning(
            "Could not capture pre-state for port %s on %s: %s",
            interface, device, exc,
        )
        return {"existed": None, "description": None}


def _get_pre_state(interface: str, device: str) -> dict:
    """Dispatch pre-state capture through ``app.api.ports`` so tests can
    monkeypatch it there — mirroring the pattern used by VLAN execution."""
    import app.api.ports as _ports_api  # lazy: avoids circular import at load time
    return _ports_api._capture_pre_state_port_description(interface, device)


def _capture_pre_state_admin(interface: str, device: str) -> dict:
    """Return the current admin state of *interface* on *device*.

    Keys:
        existed:  True / False / None  (None = could not determine)
        admin_up: True / False / None  (None when the device does not
                  expose admin state or when ``existed`` is not True)
    """
    try:
        response = port_service.list_ports(device)
        match = next((p for p in response.ports if p.name == interface), None)
        if match is None:
            return {"existed": False, "admin_up": None}
        return {"existed": True, "admin_up": match.admin_up}
    except Exception as exc:
        logger.warning(
            "Could not capture admin-state pre-state for port %s on %s: %s",
            interface, device, exc,
        )
        return {"existed": None, "admin_up": None}


def _get_admin_pre_state(interface: str, device: str) -> dict:
    """Dispatch the admin-state pre-state capture through ``app.api.ports``
    so tests can monkeypatch the attachment point.  Mirrors the description
    flow."""
    import app.api.ports as _ports_api  # lazy: avoids circular import at load time
    return _ports_api._capture_pre_state_port_admin(interface, device)


# ── Rollback ─────────────────────────────────────────────────────────────────

def _rollback_description(
    interface: str,
    device: str,
    job_id: str,
    pre_state: dict,
) -> tuple[bool, bool | None]:
    """Restore the prior description on *interface*.

    Returns ``(rollback_performed, rollback_success)``:
        * (False, None)  — no rollback triggered (no previous value or
          port did not exist before).
        * (True, False)  — rollback attempted but failed.
        * (True, True)   — rollback executed and post-state confirms restore.
    """
    if pre_state.get("existed") is not True:
        logger.info(
            "Job %s rollback skipped — port %s did not exist before operation on device=%s",
            job_id, interface, device,
        )
        return False, None

    prev_description = pre_state.get("description") or ""
    logger.info(
        "Job %s rollback started for port %s on device=%s (restore description=%r)",
        job_id, interface, device, prev_description,
    )
    job_service.update_job(job_id, current_step="rollback_started")
    _rb_t0 = time.time()

    try:
        rb = port_service.update_port_description_on_device(interface, prev_description, device)
    except Exception as exc:
        _rb_ms = round((time.time() - _rb_t0) * 1000)
        logger.error(
            "Job %s rollback exception for port %s on device=%s in %dms: %s",
            job_id, interface, device, _rb_ms, exc,
        )
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    _rb_ms = round((time.time() - _rb_t0) * 1000)
    rc = rb.get("rc", 1) if isinstance(rb, dict) else 1
    if rc != 0:
        logger.warning(
            "Job %s rollback execution failed for port %s on device=%s in %dms",
            job_id, interface, device, _rb_ms,
        )
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    logger.info(
        "Job %s rollback executed for port %s on device=%s in %dms",
        job_id, interface, device, _rb_ms,
    )

    # Verify the description was actually restored
    try:
        response = port_service.list_ports(device)
        match = next((p for p in response.ports if p.name == interface), None)
        current = (match.description or "") if match else ""
        success = current.strip() == prev_description.strip()
    except Exception as exc:
        logger.warning(
            "Job %s rollback state check failed for port %s on device=%s: %s",
            job_id, interface, device, exc,
        )
        success = False

    if success:
        logger.info(
            "Job %s rollback verification succeeded for port %s on device=%s",
            job_id, interface, device,
        )
    else:
        logger.warning(
            "Job %s rollback verification failed for port %s on device=%s",
            job_id, interface, device,
        )
    job_service.update_job(job_id, current_step="rollback_completed")
    return True, success


# ── Job runner ───────────────────────────────────────────────────────────────

def run_update_description_job(
    job_id: str,
    interface: str,
    description: str,
    device: str,
    audit_id: str,
    retry_base_delay: float = 1.0,
    pre_state: dict | None = None,
    group_job_id: str | None = None,
):
    """Execute one port-description update.  Never raises — failures are
    funnelled into the job record / audit event so the BackgroundTasks
    queue is never poisoned."""
    from app.services import device_locks, rate_limiter

    start_time = time.time()
    logger.info(
        "Job %s: queued — update description on interface=%s device=%s",
        job_id, interface, device,
    )

    try:
        _job = job_service.get_job(job_id)
        if _job and _job.status == "cancelled":
            audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
            return

        rate_limiter.wait_for_slot(device)

        with device_locks.acquire(device):
            _job = job_service.get_job(job_id)
            if _job and _job.status == "cancelled":
                audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
                return

            job_service.update_job(job_id, "running")
            logger.info(
                "Job %s: started — update description on interface=%s device=%s",
                job_id, interface, device,
            )

            if pre_state is None:
                pre_state = _get_pre_state(interface, device)
            job_service.update_job(job_id, pre_state=pre_state)

            if pre_state.get("existed") is None:
                error_msg = (
                    f"Cannot determine port state on device '{device}' — aborting operation"
                )
                logger.error(
                    "Job %s: pre-state check failed on device=%s", job_id, device,
                )
                job_service.update_job(
                    job_id, "failed", error=error_msg,
                    retry_count=0, rollback_performed=False,
                )
                audit_service.append_audit_event(audit_id, "failed", {
                    "error": {"type": "precheck_failed", "message": error_msg},
                    "error_type": "permanent",
                    "pre_state": pre_state,
                })
                return

            if pre_state.get("existed") is False:
                error_msg = f"Interface '{interface}' does not exist on device '{device}'"
                logger.warning("Job %s: validation failed — %s", job_id, error_msg)
                job_service.update_job(job_id, "failed", error=error_msg)
                audit_service.append_audit_event(audit_id, "failed", {
                    "validation": "failed",
                    "reason": "interface_not_found",
                    "error": {"type": "validation_error", "message": error_msg},
                    "pre_state": pre_state,
                })
                return

            # Idempotency: if the description already matches, complete as a no-op.
            prev = (pre_state.get("description") or "").strip()
            desired = (description or "").strip()
            if prev == desired:
                duration = time.time() - start_time
                logger.info(
                    "Job %s: no-op — description on %s already matches on device=%s",
                    job_id, interface, device,
                )
                job_service.update_job(
                    job_id, "completed",
                    result={
                        "output": "Description already matches requested value, no changes needed",
                        "operation_result": "noop",
                        "message": "Description unchanged (no changes needed)",
                    },
                    current_step="completed",
                )
                audit_service.append_audit_event(audit_id, "completed", {
                    "reason": "description_unchanged_no_op",
                    "duration_seconds": round(duration, 2),
                    "pre_state": pre_state,
                })
                return

            result, retry_count = _execute_with_retry(
                lambda: port_service.update_port_description_on_device(interface, description, device),
                job_id,
                retry_base_delay=retry_base_delay,
                device=device,
            )

            duration = time.time() - start_time

            if result["rc"] != 0:
                rollback_performed, rollback_success = _rollback_description(
                    interface, device, job_id, pre_state,
                )

                error_msg = result.get("stderr") or result.get("stdout") or "Execution failed"
                error_output = _combined_error(result)
                logger.error(
                    "Job %s: failed — update description on interface=%s device=%s: rc=%d error=%s",
                    job_id, interface, device, result.get("rc", -1), error_output.strip()[:300],
                )
                job_service.update_job(
                    job_id, "failed", error=error_msg,
                    retry_count=retry_count, rollback_performed=rollback_performed,
                    rollback_success=rollback_success,
                    current_step="rollback_completed" if rollback_performed else None,
                )
                audit_service.append_audit_event(audit_id, "failed", {
                    "retries": retry_count,
                    "rollback_performed": rollback_performed,
                    "rollback_success": rollback_success,
                    "duration_seconds": round(duration, 2),
                    "error": _structured_error(result),
                    "error_type": _classify_error(result),
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
                    "Job %s: completed — description updated on %s/%s in %.2fs (retries=%d)",
                    job_id, device, interface, duration, retry_count,
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
        if group_job_id:
            _notify_group_job_complete(group_job_id, job_id, device)


# ── Enqueue ──────────────────────────────────────────────────────────────────

def enqueue_update_description_job(
    interface: str,
    description: str,
    device: str,
    username: str,
    background_tasks: BackgroundTasks,
    retry_base_delay: float = 1.0,
) -> tuple[list[dict], str]:
    """Create job + group-job records and schedule the runner.

    Returns ``(job_entries, group_job_id)`` to match the response shape
    VLAN endpoints already use, so the frontend's JobNotificationContext
    can track this without any new wiring.
    """
    from app.services import device_locks

    request_id = str(uuid.uuid4())
    group_job = group_job_service.create_group_job(
        operation="update_port_description",
        playbook="update_port_description.yml",
        parameters={"interface": interface, "description": description},
        devices=[device],
    )

    job = job_service.create_job(
        playbook="update_port_description.yml",
        device=device,
        parameters={"interface": interface, "description": description},
        group_job_id=group_job.group_job_id,
    )
    audit = audit_service.log_action(
        user=username,
        action="update_port_description",
        resource="port",
        details={"interface": interface, "description": description, "device": device},
        status="pending",
        job_id=job.job_id,
        device=device,
        request_id=request_id,
    )

    try:
        with device_locks.acquire(device, timeout=30):
            pre_state = _get_pre_state(interface, device)
    except TimeoutError:
        logger.warning(
            "Device %s busy during pre-state capture for port %s — job will abort on start",
            device, interface,
        )
        pre_state = {"existed": None, "description": None}

    background_tasks.add_task(
        run_update_description_job,
        job.job_id,
        interface,
        description,
        device,
        audit.id,
        retry_base_delay,
        pre_state,
        group_job.group_job_id,
    )

    return (
        [{"device": device, "job_id": job.job_id, "status": job.status}],
        group_job.group_job_id,
    )


# ── Admin-state orchestration (Step 2.2) ─────────────────────────────────────

def _rollback_admin_state(
    interface: str,
    device: str,
    job_id: str,
    pre_state: dict,
) -> tuple[bool, bool | None]:
    """Restore the prior admin state on *interface*.

    Returns ``(rollback_performed, rollback_success)`` following the same
    contract used by the description rollback path.
    """
    if pre_state.get("existed") is not True:
        logger.info(
            "Job %s rollback skipped — port %s did not exist before operation on device=%s",
            job_id, interface, device,
        )
        return False, None

    prev_admin = pre_state.get("admin_up")
    if prev_admin is None:
        # We never read the prior value (some platforms don't expose admin
        # state in the inventory).  Rolling back blindly could put the port
        # into a worse state than where we found it; safer to no-op.
        logger.info(
            "Job %s rollback skipped — prior admin state unknown for port %s on device=%s",
            job_id, interface, device,
        )
        return False, None

    logger.info(
        "Job %s rollback started for port %s on device=%s (restore admin_up=%s)",
        job_id, interface, device, prev_admin,
    )
    job_service.update_job(job_id, current_step="rollback_started")
    _rb_t0 = time.time()

    try:
        rb = port_service.set_port_admin_state_on_device(interface, bool(prev_admin), device)
    except Exception as exc:
        _rb_ms = round((time.time() - _rb_t0) * 1000)
        logger.error(
            "Job %s rollback exception for port %s on device=%s in %dms: %s",
            job_id, interface, device, _rb_ms, exc,
        )
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    _rb_ms = round((time.time() - _rb_t0) * 1000)
    rc = rb.get("rc", 1) if isinstance(rb, dict) else 1
    if rc != 0:
        logger.warning(
            "Job %s rollback execution failed for port %s on device=%s in %dms",
            job_id, interface, device, _rb_ms,
        )
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    logger.info(
        "Job %s rollback executed for port %s on device=%s in %dms",
        job_id, interface, device, _rb_ms,
    )

    # Verify the admin state was actually restored
    try:
        response = port_service.list_ports(device)
        match = next((p for p in response.ports if p.name == interface), None)
        current = match.admin_up if match else None
        success = (current is not None) and (bool(current) == bool(prev_admin))
    except Exception as exc:
        logger.warning(
            "Job %s rollback state check failed for port %s on device=%s: %s",
            job_id, interface, device, exc,
        )
        success = False

    if success:
        logger.info(
            "Job %s rollback verification succeeded for port %s on device=%s",
            job_id, interface, device,
        )
    else:
        logger.warning(
            "Job %s rollback verification failed for port %s on device=%s",
            job_id, interface, device,
        )
    job_service.update_job(job_id, current_step="rollback_completed")
    return True, success


def run_set_admin_state_job(
    job_id: str,
    interface: str,
    enabled: bool,
    device: str,
    audit_id: str,
    retry_base_delay: float = 1.0,
    pre_state: dict | None = None,
    group_job_id: str | None = None,
):
    """Execute one admin-state change.  Never raises — failures funnel into
    the job record / audit event."""
    from app.services import device_locks, rate_limiter

    start_time = time.time()
    logger.info(
        "Job %s: queued — set admin state on interface=%s device=%s enabled=%s",
        job_id, interface, device, enabled,
    )

    try:
        _job = job_service.get_job(job_id)
        if _job and _job.status == "cancelled":
            audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
            return

        rate_limiter.wait_for_slot(device)

        with device_locks.acquire(device):
            _job = job_service.get_job(job_id)
            if _job and _job.status == "cancelled":
                audit_service.append_audit_event(audit_id, "cancelled", {"reason": "cancelled_before_execution"})
                return

            job_service.update_job(job_id, "running")
            logger.info(
                "Job %s: started — set admin state on interface=%s device=%s enabled=%s",
                job_id, interface, device, enabled,
            )

            if pre_state is None:
                pre_state = _get_admin_pre_state(interface, device)
            job_service.update_job(job_id, pre_state=pre_state)

            if pre_state.get("existed") is None:
                error_msg = (
                    f"Cannot determine port state on device '{device}' — aborting operation"
                )
                logger.error(
                    "Job %s: pre-state check failed on device=%s", job_id, device,
                )
                job_service.update_job(
                    job_id, "failed", error=error_msg,
                    retry_count=0, rollback_performed=False,
                )
                audit_service.append_audit_event(audit_id, "failed", {
                    "error": {"type": "precheck_failed", "message": error_msg},
                    "error_type": "permanent",
                    "pre_state": pre_state,
                })
                return

            if pre_state.get("existed") is False:
                error_msg = f"Interface '{interface}' does not exist on device '{device}'"
                logger.warning("Job %s: validation failed — %s", job_id, error_msg)
                job_service.update_job(job_id, "failed", error=error_msg)
                audit_service.append_audit_event(audit_id, "failed", {
                    "validation": "failed",
                    "reason": "interface_not_found",
                    "error": {"type": "validation_error", "message": error_msg},
                    "pre_state": pre_state,
                })
                return

            # Idempotency: if admin state already matches, complete as a no-op.
            prev_admin = pre_state.get("admin_up")
            if prev_admin is not None and bool(prev_admin) == bool(enabled):
                duration = time.time() - start_time
                logger.info(
                    "Job %s: no-op — admin state on %s already %s on device=%s",
                    job_id, interface, "enabled" if enabled else "disabled", device,
                )
                job_service.update_job(
                    job_id, "completed",
                    result={
                        "output": "Admin state already matches requested value, no changes needed",
                        "operation_result": "noop",
                        "message": "Admin state unchanged (no changes needed)",
                    },
                    current_step="completed",
                )
                audit_service.append_audit_event(audit_id, "completed", {
                    "reason": "admin_state_unchanged_no_op",
                    "duration_seconds": round(duration, 2),
                    "pre_state": pre_state,
                })
                return

            result, retry_count = _execute_with_retry(
                lambda: port_service.set_port_admin_state_on_device(interface, enabled, device),
                job_id,
                retry_base_delay=retry_base_delay,
                device=device,
            )

            duration = time.time() - start_time

            if result["rc"] != 0:
                rollback_performed, rollback_success = _rollback_admin_state(
                    interface, device, job_id, pre_state,
                )

                error_msg = result.get("stderr") or result.get("stdout") or "Execution failed"
                error_output = _combined_error(result)
                logger.error(
                    "Job %s: failed — set admin state on interface=%s device=%s: rc=%d error=%s",
                    job_id, interface, device, result.get("rc", -1), error_output.strip()[:300],
                )
                job_service.update_job(
                    job_id, "failed", error=error_msg,
                    retry_count=retry_count, rollback_performed=rollback_performed,
                    rollback_success=rollback_success,
                    current_step="rollback_completed" if rollback_performed else None,
                )
                audit_service.append_audit_event(audit_id, "failed", {
                    "retries": retry_count,
                    "rollback_performed": rollback_performed,
                    "rollback_success": rollback_success,
                    "duration_seconds": round(duration, 2),
                    "error": _structured_error(result),
                    "error_type": _classify_error(result),
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
                    "Job %s: completed — admin state on %s/%s set to %s in %.2fs (retries=%d)",
                    job_id, device, interface,
                    "enabled" if enabled else "disabled",
                    duration, retry_count,
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
        if group_job_id:
            _notify_group_job_complete(group_job_id, job_id, device)


def enqueue_set_admin_state_job(
    interface: str,
    enabled: bool,
    device: str,
    username: str,
    background_tasks: BackgroundTasks,
    retry_base_delay: float = 1.0,
) -> tuple[list[dict], str]:
    """Create job + group-job records and schedule the admin-state runner."""
    from app.services import device_locks

    request_id = str(uuid.uuid4())
    action = "enable_port" if enabled else "disable_port"
    group_job = group_job_service.create_group_job(
        operation=action,
        playbook="set_port_admin_state.yml",
        parameters={"interface": interface, "enabled": enabled},
        devices=[device],
    )

    job = job_service.create_job(
        playbook="set_port_admin_state.yml",
        device=device,
        parameters={"interface": interface, "enabled": enabled},
        group_job_id=group_job.group_job_id,
    )
    audit = audit_service.log_action(
        user=username,
        action=action,
        resource="port",
        details={"interface": interface, "enabled": enabled, "device": device},
        status="pending",
        job_id=job.job_id,
        device=device,
        request_id=request_id,
    )

    try:
        with device_locks.acquire(device, timeout=30):
            pre_state = _get_admin_pre_state(interface, device)
    except TimeoutError:
        logger.warning(
            "Device %s busy during pre-state capture for port %s — job will abort on start",
            device, interface,
        )
        pre_state = {"existed": None, "admin_up": None}

    background_tasks.add_task(
        run_set_admin_state_job,
        job.job_id,
        interface,
        bool(enabled),
        device,
        audit.id,
        retry_base_delay,
        pre_state,
        group_job.group_job_id,
    )

    return (
        [{"device": device, "job_id": job.job_id, "status": job.status}],
        group_job.group_job_id,
    )
