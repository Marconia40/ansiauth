"""Orchestration for composite and semantic port write operations (Step 3.3).

Implements three new orchestration paths that wire the Step 3.1/3.2 driver
methods into the full execution model:

* configure_port  — composite multi-field port mutation via a single driver call
* shutdown_port   — intent-named administrative disable
* enable_port     — intent-named administrative enable

All three reuse the helpers from vlan_execution_service (retry classification,
error shaping) and follow the same pattern as port_execution_service:
pre-state capture → rate-limit → device lock → execute-with-retry → rollback
on failure → audit chain → group-job notification.

shutdown_port and enable_port share the same pre-state shape as the admin-state
flow but dispatch to the explicit ``shutdown_port`` / ``enable_port`` driver
methods.  configure_port captures a richer pre-state (all mutable fields) and
performs a field-selective rollback on failure using a reconstructed
PortConfigRequest.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING

from fastapi import BackgroundTasks

from app.models.port import PortConfigRequest
from app.services import (
    audit_service,
    group_job_service,
    job_service,
    port_service,
    vlan_execution_service,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_classify_result = vlan_execution_service._classify_result
_combined_error = vlan_execution_service._combined_error
_classify_error = vlan_execution_service.classify_error
_structured_error = vlan_execution_service._structured_error
_execute_with_retry = vlan_execution_service._execute_with_retry
_notify_group_job_complete = vlan_execution_service._notify_group_job_complete


# ── Pre-state capture ─────────────────────────────────────────────────────────

def _capture_pre_state_configure(interface: str, device: str) -> dict:
    """Capture the full mutable state of *interface* for configure_port rollback.

    Keys:
        existed:       True / False / None  (None = could not determine)
        description:   str / None
        admin_up:      bool / None
        mode:          'access' / 'trunk' / 'unknown' / None
        access_vlan:   int / None
        allowed_vlans: list[int] / None
    """
    try:
        response = port_service.list_ports(device)
        match = next((p for p in response.ports if p.name == interface), None)
        if match is None:
            return {
                "existed": False,
                "description": None, "admin_up": None,
                "mode": None, "access_vlan": None, "allowed_vlans": None,
            }
        return {
            "existed": True,
            "description": match.description,
            "admin_up": match.admin_up,
            "mode": match.mode,
            "access_vlan": match.access_vlan,
            "allowed_vlans": (
                list(match.allowed_vlans) if match.allowed_vlans is not None else None
            ),
        }
    except Exception as exc:
        logger.warning(
            "Could not capture configure pre-state for port %s on %s: %s",
            interface, device, exc,
        )
        return {
            "existed": None,
            "description": None, "admin_up": None,
            "mode": None, "access_vlan": None, "allowed_vlans": None,
        }


def _get_configure_pre_state(interface: str, device: str) -> dict:
    """Dispatch configure pre-state capture through app.api.ports for test patchability."""
    import app.api.ports as _ports_api
    return _ports_api._capture_pre_state_port_configure(interface, device)


def _capture_pre_state_shutdown(interface: str, device: str) -> dict:
    """Return the current admin state of *interface* (same shape as admin-state pre-state).

    Keys: existed (True/False/None), admin_up (bool/None)
    """
    from app.services.port_execution_service import _capture_pre_state_admin
    return _capture_pre_state_admin(interface, device)


def _get_shutdown_pre_state(interface: str, device: str) -> dict:
    import app.api.ports as _ports_api
    return _ports_api._capture_pre_state_port_shutdown(interface, device)


def _capture_pre_state_enable(interface: str, device: str) -> dict:
    from app.services.port_execution_service import _capture_pre_state_admin
    return _capture_pre_state_admin(interface, device)


def _get_enable_pre_state(interface: str, device: str) -> dict:
    import app.api.ports as _ports_api
    return _ports_api._capture_pre_state_port_enable(interface, device)


# ── Idempotency check ─────────────────────────────────────────────────────────

def _is_configure_noop(config: PortConfigRequest, pre_state: dict) -> bool:
    """Return True only when every requested mutation field already matches pre-state."""
    if pre_state.get("existed") is not True:
        return False

    checks: list[bool] = []

    if "description" in config.mutation_fields:
        desired = (config.description or "").strip()
        current = (pre_state.get("description") or "").strip()
        checks.append(desired == current)

    if "admin_enabled" in config.mutation_fields:
        prev = pre_state.get("admin_up")
        if prev is None:
            return False
        checks.append(bool(prev) == bool(config.admin_enabled))

    if "mode" in config.mutation_fields:
        checks.append(pre_state.get("mode") == config.mode)

    if "access_vlan" in config.mutation_fields:
        checks.append(pre_state.get("access_vlan") == config.access_vlan)

    if "allowed_vlans" in config.mutation_fields:
        current_vlans = pre_state.get("allowed_vlans")
        if current_vlans is None:
            return False
        checks.append(sorted(current_vlans) == sorted(config.allowed_vlans))

    return bool(checks) and all(checks)


# ── Rollback ──────────────────────────────────────────────────────────────────

def _rollback_configure_port(
    config: PortConfigRequest,
    device: str,
    job_id: str,
    pre_state: dict,
) -> tuple[bool, bool | None]:
    """Restore the fields mutated by a failed configure_port back to pre-state values.

    Constructs a rollback PortConfigRequest covering only the fields that were
    in the original request and have a known pre-state value.  Returns
    ``(rollback_performed, rollback_success)``.
    """
    if pre_state.get("existed") is not True:
        logger.info(
            "Job %s configure rollback skipped — port %s did not exist before operation on device=%s",
            job_id, config.interface, device,
        )
        return False, None

    rb_kwargs: dict = {"device": device, "interface": config.interface}

    if "description" in config.mutation_fields:
        rb_kwargs["description"] = pre_state.get("description") or ""

    if "admin_enabled" in config.mutation_fields:
        prev_admin = pre_state.get("admin_up")
        if prev_admin is not None:
            rb_kwargs["admin_enabled"] = bool(prev_admin)

    if "mode" in config.mutation_fields:
        prev_mode = pre_state.get("mode")
        if prev_mode in ("access", "trunk"):
            rb_kwargs["mode"] = prev_mode
            if prev_mode == "access":
                prev_vlan = pre_state.get("access_vlan")
                if prev_vlan is not None:
                    rb_kwargs["access_vlan"] = int(prev_vlan)
            else:
                prev_vlans = pre_state.get("allowed_vlans")
                if prev_vlans:
                    rb_kwargs["allowed_vlans"] = list(prev_vlans)
    elif "access_vlan" in config.mutation_fields:
        prev_vlan = pre_state.get("access_vlan")
        if prev_vlan is not None:
            rb_kwargs["access_vlan"] = int(prev_vlan)
            rb_kwargs.setdefault("mode", "access")
    elif "allowed_vlans" in config.mutation_fields:
        prev_vlans = pre_state.get("allowed_vlans")
        if prev_vlans is not None:
            rb_kwargs["allowed_vlans"] = list(prev_vlans)
            rb_kwargs.setdefault("mode", "trunk")

    mutation_keys = {k for k in rb_kwargs if k not in ("device", "interface")}
    if not mutation_keys:
        logger.info(
            "Job %s configure rollback skipped — all pre-state values unknown for port %s on device=%s",
            job_id, config.interface, device,
        )
        return False, None

    try:
        rollback_config = PortConfigRequest(**rb_kwargs)
    except ValueError as exc:
        logger.warning(
            "Job %s configure rollback skipped — cannot build rollback request for port %s: %s",
            job_id, config.interface, exc,
        )
        return False, None

    logger.info(
        "Job %s configure rollback started for port %s on device=%s (restoring fields=%s)",
        job_id, config.interface, device, sorted(mutation_keys),
    )
    job_service.update_job(job_id, current_step="rollback_started")
    _rb_t0 = time.time()

    try:
        rb = port_service.configure_port_on_device(rollback_config, device)
        _rb_ms = round((time.time() - _rb_t0) * 1000)
        success = rb.success
        if success:
            logger.info(
                "Job %s configure rollback succeeded for port %s on device=%s in %dms",
                job_id, config.interface, device, _rb_ms,
            )
        else:
            logger.warning(
                "Job %s configure rollback FAILED for port %s on device=%s in %dms",
                job_id, config.interface, device, _rb_ms,
            )
    except Exception as exc:
        _rb_ms = round((time.time() - _rb_t0) * 1000)
        logger.error(
            "Job %s configure rollback exception for port %s on device=%s in %dms: %s",
            job_id, config.interface, device, _rb_ms, exc,
        )
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    job_service.update_job(job_id, current_step="rollback_completed")
    return True, success


def _rollback_shutdown_port(
    interface: str,
    device: str,
    job_id: str,
    pre_state: dict,
) -> tuple[bool, bool | None]:
    """Restore admin state after a failed shutdown_port.

    Calls enable_port_on_device when the pre-state confirms the port was up.
    Skips rollback when the port was already down (shutdown would be idempotent).
    """
    if pre_state.get("existed") is not True:
        logger.info(
            "Job %s shutdown rollback skipped — port %s did not exist on device=%s",
            job_id, interface, device,
        )
        return False, None

    prev_admin = pre_state.get("admin_up")
    if prev_admin is None:
        logger.info(
            "Job %s shutdown rollback skipped — prior admin state unknown for port %s on device=%s",
            job_id, interface, device,
        )
        return False, None

    if not bool(prev_admin):
        logger.info(
            "Job %s shutdown rollback skipped — port %s was already down on device=%s",
            job_id, interface, device,
        )
        return False, None

    logger.info(
        "Job %s shutdown rollback started for port %s on device=%s (restore admin_up=True)",
        job_id, interface, device,
    )
    job_service.update_job(job_id, current_step="rollback_started")
    _rb_t0 = time.time()

    try:
        rb = port_service.enable_port_on_device(interface, device)
    except Exception as exc:
        _rb_ms = round((time.time() - _rb_t0) * 1000)
        logger.error(
            "Job %s shutdown rollback exception for port %s on device=%s in %dms: %s",
            job_id, interface, device, _rb_ms, exc,
        )
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    _rb_ms = round((time.time() - _rb_t0) * 1000)
    rc = rb.get("rc", 1) if isinstance(rb, dict) else 1
    success = rc == 0
    if success:
        logger.info(
            "Job %s shutdown rollback succeeded for port %s on device=%s in %dms",
            job_id, interface, device, _rb_ms,
        )
    else:
        logger.warning(
            "Job %s shutdown rollback FAILED for port %s on device=%s in %dms",
            job_id, interface, device, _rb_ms,
        )
    job_service.update_job(job_id, current_step="rollback_completed")
    return True, success


def _rollback_enable_port(
    interface: str,
    device: str,
    job_id: str,
    pre_state: dict,
) -> tuple[bool, bool | None]:
    """Restore admin state after a failed enable_port.

    Calls shutdown_port_on_device when the pre-state confirms the port was down.
    Skips rollback when the port was already up (enable would be idempotent).
    """
    if pre_state.get("existed") is not True:
        logger.info(
            "Job %s enable rollback skipped — port %s did not exist on device=%s",
            job_id, interface, device,
        )
        return False, None

    prev_admin = pre_state.get("admin_up")
    if prev_admin is None:
        logger.info(
            "Job %s enable rollback skipped — prior admin state unknown for port %s on device=%s",
            job_id, interface, device,
        )
        return False, None

    if bool(prev_admin):
        logger.info(
            "Job %s enable rollback skipped — port %s was already up on device=%s",
            job_id, interface, device,
        )
        return False, None

    logger.info(
        "Job %s enable rollback started for port %s on device=%s (restore admin_up=False)",
        job_id, interface, device,
    )
    job_service.update_job(job_id, current_step="rollback_started")
    _rb_t0 = time.time()

    try:
        rb = port_service.shutdown_port_on_device(interface, device)
    except Exception as exc:
        _rb_ms = round((time.time() - _rb_t0) * 1000)
        logger.error(
            "Job %s enable rollback exception for port %s on device=%s in %dms: %s",
            job_id, interface, device, _rb_ms, exc,
        )
        job_service.update_job(job_id, current_step="rollback_completed")
        return True, False

    _rb_ms = round((time.time() - _rb_t0) * 1000)
    rc = rb.get("rc", 1) if isinstance(rb, dict) else 1
    success = rc == 0
    if success:
        logger.info(
            "Job %s enable rollback succeeded for port %s on device=%s in %dms",
            job_id, interface, device, _rb_ms,
        )
    else:
        logger.warning(
            "Job %s enable rollback FAILED for port %s on device=%s in %dms",
            job_id, interface, device, _rb_ms,
        )
    job_service.update_job(job_id, current_step="rollback_completed")
    return True, success


# ── Job runners ───────────────────────────────────────────────────────────────

def run_configure_port_job(
    job_id: str,
    config: PortConfigRequest,
    device: str,
    audit_id: str,
    retry_base_delay: float = 1.0,
    pre_state: dict | None = None,
    group_job_id: str | None = None,
):
    """Execute one configure_port operation.  Never raises — failures funnel
    into the job record / audit event so BackgroundTasks is never poisoned."""
    from app.services import device_locks, rate_limiter

    start_time = time.time()
    logger.info(
        "Job %s: queued — configure_port on interface=%s device=%s fields=%s",
        job_id, config.interface, device, config.mutation_fields,
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
                "Job %s: started — configure_port on interface=%s device=%s fields=%s",
                job_id, config.interface, device, config.mutation_fields,
            )

            if pre_state is None:
                pre_state = _get_configure_pre_state(config.interface, device)
            job_service.update_job(job_id, pre_state=pre_state)

            if pre_state.get("existed") is None:
                error_msg = (
                    f"Cannot determine port state on device '{device}' — aborting operation"
                )
                logger.error("Job %s: pre-state check failed on device=%s", job_id, device)
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
                error_msg = f"Interface '{config.interface}' does not exist on device '{device}'"
                logger.warning("Job %s: validation failed — %s", job_id, error_msg)
                job_service.update_job(job_id, "failed", error=error_msg)
                audit_service.append_audit_event(audit_id, "failed", {
                    "validation": "failed",
                    "reason": "interface_not_found",
                    "error": {"type": "validation_error", "message": error_msg},
                    "pre_state": pre_state,
                })
                return

            if _is_configure_noop(config, pre_state):
                duration = time.time() - start_time
                logger.info(
                    "Job %s: no-op — all requested fields on %s already match on device=%s",
                    job_id, config.interface, device,
                )
                job_service.update_job(
                    job_id, "completed",
                    result={
                        "output": "All requested port fields already match, no changes needed",
                        "operation_result": "noop",
                        "message": "Port configuration unchanged (no changes needed)",
                    },
                    current_step="completed",
                )
                audit_service.append_audit_event(audit_id, "completed", {
                    "reason": "configure_port_noop",
                    "duration_seconds": round(duration, 2),
                    "pre_state": pre_state,
                    "fields": config.mutation_fields,
                })
                return

            def _do_configure() -> dict:
                result = port_service.configure_port_on_device(config, device)
                return {
                    "rc": 0 if result.success else 1,
                    "stdout": (
                        f"configure_port interface={result.interface} "
                        f"changed={result.changed} "
                        f"time={result.execution_time_ms}ms"
                    ),
                    "stderr": (
                        "" if result.success
                        else f"configure_port failed on {result.interface}"
                    ),
                    "success": result.success,
                }

            result, retry_count = _execute_with_retry(
                _do_configure,
                job_id,
                retry_base_delay=retry_base_delay,
                device=device,
            )

            duration = time.time() - start_time

            if result["rc"] != 0:
                rollback_performed, rollback_success = _rollback_configure_port(
                    config, device, job_id, pre_state,
                )
                error_msg = result.get("stderr") or result.get("stdout") or "Execution failed"
                error_output = _combined_error(result)
                logger.error(
                    "Job %s: failed — configure_port on interface=%s device=%s: rc=%d error=%s",
                    job_id, config.interface, device, result.get("rc", -1),
                    error_output.strip()[:300],
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
                    "fields": config.mutation_fields,
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
                    "fields": config.mutation_fields,
                })
                logger.info(
                    "Job %s: completed — configure_port on %s/%s in %.2fs (retries=%d fields=%s)",
                    job_id, device, config.interface, duration, retry_count,
                    config.mutation_fields,
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


def run_shutdown_port_job(
    job_id: str,
    interface: str,
    device: str,
    audit_id: str,
    retry_base_delay: float = 1.0,
    pre_state: dict | None = None,
    group_job_id: str | None = None,
):
    """Execute one shutdown_port operation.  Never raises."""
    from app.services import device_locks, rate_limiter

    start_time = time.time()
    logger.info(
        "Job %s: queued — shutdown_port on interface=%s device=%s",
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
                "Job %s: started — shutdown_port on interface=%s device=%s",
                job_id, interface, device,
            )

            if pre_state is None:
                pre_state = _get_shutdown_pre_state(interface, device)
            job_service.update_job(job_id, pre_state=pre_state)

            if pre_state.get("existed") is None:
                error_msg = (
                    f"Cannot determine port state on device '{device}' — aborting operation"
                )
                logger.error("Job %s: pre-state check failed on device=%s", job_id, device)
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

            prev_admin = pre_state.get("admin_up")
            if prev_admin is not None and not bool(prev_admin):
                duration = time.time() - start_time
                logger.info(
                    "Job %s: no-op — port %s already shut down on device=%s",
                    job_id, interface, device,
                )
                job_service.update_job(
                    job_id, "completed",
                    result={
                        "output": "Port is already administratively down, no changes needed",
                        "operation_result": "noop",
                        "message": "Port already shut down (no changes needed)",
                    },
                    current_step="completed",
                )
                audit_service.append_audit_event(audit_id, "completed", {
                    "reason": "shutdown_port_noop",
                    "duration_seconds": round(duration, 2),
                    "pre_state": pre_state,
                })
                return

            result, retry_count = _execute_with_retry(
                lambda: port_service.shutdown_port_on_device(interface, device),
                job_id,
                retry_base_delay=retry_base_delay,
                device=device,
            )

            duration = time.time() - start_time

            if result["rc"] != 0:
                rollback_performed, rollback_success = _rollback_shutdown_port(
                    interface, device, job_id, pre_state,
                )
                error_msg = result.get("stderr") or result.get("stdout") or "Execution failed"
                error_output = _combined_error(result)
                logger.error(
                    "Job %s: failed — shutdown_port on interface=%s device=%s: rc=%d error=%s",
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
                    "Job %s: completed — shutdown_port on %s/%s in %.2fs (retries=%d)",
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


def run_enable_port_job(
    job_id: str,
    interface: str,
    device: str,
    audit_id: str,
    retry_base_delay: float = 1.0,
    pre_state: dict | None = None,
    group_job_id: str | None = None,
):
    """Execute one enable_port operation.  Never raises."""
    from app.services import device_locks, rate_limiter

    start_time = time.time()
    logger.info(
        "Job %s: queued — enable_port on interface=%s device=%s",
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
                "Job %s: started — enable_port on interface=%s device=%s",
                job_id, interface, device,
            )

            if pre_state is None:
                pre_state = _get_enable_pre_state(interface, device)
            job_service.update_job(job_id, pre_state=pre_state)

            if pre_state.get("existed") is None:
                error_msg = (
                    f"Cannot determine port state on device '{device}' — aborting operation"
                )
                logger.error("Job %s: pre-state check failed on device=%s", job_id, device)
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

            prev_admin = pre_state.get("admin_up")
            if prev_admin is not None and bool(prev_admin):
                duration = time.time() - start_time
                logger.info(
                    "Job %s: no-op — port %s already enabled on device=%s",
                    job_id, interface, device,
                )
                job_service.update_job(
                    job_id, "completed",
                    result={
                        "output": "Port is already administratively up, no changes needed",
                        "operation_result": "noop",
                        "message": "Port already enabled (no changes needed)",
                    },
                    current_step="completed",
                )
                audit_service.append_audit_event(audit_id, "completed", {
                    "reason": "enable_port_noop",
                    "duration_seconds": round(duration, 2),
                    "pre_state": pre_state,
                })
                return

            result, retry_count = _execute_with_retry(
                lambda: port_service.enable_port_on_device(interface, device),
                job_id,
                retry_base_delay=retry_base_delay,
                device=device,
            )

            duration = time.time() - start_time

            if result["rc"] != 0:
                rollback_performed, rollback_success = _rollback_enable_port(
                    interface, device, job_id, pre_state,
                )
                error_msg = result.get("stderr") or result.get("stdout") or "Execution failed"
                error_output = _combined_error(result)
                logger.error(
                    "Job %s: failed — enable_port on interface=%s device=%s: rc=%d error=%s",
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
                    "Job %s: completed — enable_port on %s/%s in %.2fs (retries=%d)",
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


# ── Enqueue (public API) ──────────────────────────────────────────────────────

def configure_port(
    config: PortConfigRequest,
    username: str,
    background_tasks: BackgroundTasks,
    retry_base_delay: float = 1.0,
) -> tuple[list[dict], str]:
    """Create job + group-job records and schedule configure_port.

    Returns ``(job_entries, group_job_id)`` matching the response shape used
    by all other port mutation endpoints.
    """
    from app.services import device_locks

    device = config.device
    request_id = str(uuid.uuid4())
    group_job = group_job_service.create_group_job(
        operation="configure_port",
        playbook="configure_port.yml",
        parameters={"interface": config.interface, "fields": config.mutation_fields},
        devices=[device],
    )
    job = job_service.create_job(
        playbook="configure_port.yml",
        device=device,
        parameters={"interface": config.interface, "fields": config.mutation_fields},
        group_job_id=group_job.group_job_id,
    )
    audit = audit_service.log_action(
        user=username,
        action="configure_port",
        resource="port",
        details={
            "interface": config.interface, "device": device,
            "fields": config.mutation_fields,
        },
        status="pending",
        job_id=job.job_id,
        device=device,
        request_id=request_id,
    )

    try:
        with device_locks.acquire(device, timeout=30):
            pre_state = _get_configure_pre_state(config.interface, device)
    except TimeoutError:
        logger.warning(
            "Device %s busy during pre-state capture for configure_port on port %s",
            device, config.interface,
        )
        pre_state = {
            "existed": None,
            "description": None, "admin_up": None,
            "mode": None, "access_vlan": None, "allowed_vlans": None,
        }

    background_tasks.add_task(
        run_configure_port_job,
        job.job_id,
        config,
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


def shutdown_port(
    interface: str,
    device: str,
    username: str,
    background_tasks: BackgroundTasks,
    retry_base_delay: float = 1.0,
) -> tuple[list[dict], str]:
    """Create job + group-job records and schedule shutdown_port."""
    from app.services import device_locks

    request_id = str(uuid.uuid4())
    group_job = group_job_service.create_group_job(
        operation="shutdown_port",
        playbook="shutdown_port.yml",
        parameters={"interface": interface},
        devices=[device],
    )
    job = job_service.create_job(
        playbook="shutdown_port.yml",
        device=device,
        parameters={"interface": interface},
        group_job_id=group_job.group_job_id,
    )
    audit = audit_service.log_action(
        user=username,
        action="shutdown_port",
        resource="port",
        details={"interface": interface, "device": device},
        status="pending",
        job_id=job.job_id,
        device=device,
        request_id=request_id,
    )

    try:
        with device_locks.acquire(device, timeout=30):
            pre_state = _get_shutdown_pre_state(interface, device)
    except TimeoutError:
        logger.warning(
            "Device %s busy during pre-state capture for shutdown_port on port %s",
            device, interface,
        )
        pre_state = {"existed": None, "admin_up": None}

    background_tasks.add_task(
        run_shutdown_port_job,
        job.job_id,
        interface,
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


def enable_port(
    interface: str,
    device: str,
    username: str,
    background_tasks: BackgroundTasks,
    retry_base_delay: float = 1.0,
) -> tuple[list[dict], str]:
    """Create job + group-job records and schedule enable_port."""
    from app.services import device_locks

    request_id = str(uuid.uuid4())
    group_job = group_job_service.create_group_job(
        operation="enable_port",
        playbook="enable_port.yml",
        parameters={"interface": interface},
        devices=[device],
    )
    job = job_service.create_job(
        playbook="enable_port.yml",
        device=device,
        parameters={"interface": interface},
        group_job_id=group_job.group_job_id,
    )
    audit = audit_service.log_action(
        user=username,
        action="enable_port",
        resource="port",
        details={"interface": interface, "device": device},
        status="pending",
        job_id=job.job_id,
        device=device,
        request_id=request_id,
    )

    try:
        with device_locks.acquire(device, timeout=30):
            pre_state = _get_enable_pre_state(interface, device)
    except TimeoutError:
        logger.warning(
            "Device %s busy during pre-state capture for enable_port on port %s",
            device, interface,
        )
        pre_state = {"existed": None, "admin_up": None}

    background_tasks.add_task(
        run_enable_port_job,
        job.job_id,
        interface,
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
