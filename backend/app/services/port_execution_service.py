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


def _capture_pre_state_access_vlan(interface: str, device: str) -> dict:
    """Return the current mode and access VLAN of *interface* on *device*.

    Keys:
        existed:     True / False / None  (None = could not determine)
        mode:        'access' / 'trunk' / 'unknown' / None
        access_vlan: int / None
    """
    try:
        response = port_service.list_ports(device)
        match = next((p for p in response.ports if p.name == interface), None)
        if match is None:
            return {"existed": False, "mode": None, "access_vlan": None}
        return {
            "existed": True,
            "mode": match.mode,
            "access_vlan": match.access_vlan,
        }
    except Exception as exc:
        logger.warning(
            "Could not capture access-vlan pre-state for port %s on %s: %s",
            interface, device, exc,
        )
        return {"existed": None, "mode": None, "access_vlan": None}


def _get_access_vlan_pre_state(interface: str, device: str) -> dict:
    """Dispatch the access-vlan pre-state capture through ``app.api.ports``
    so tests can monkeypatch the single attachment point."""
    import app.api.ports as _ports_api  # lazy: avoids circular import at load time
    return _ports_api._capture_pre_state_port_access_vlan(interface, device)


def _capture_pre_state_trunk_vlans(interface: str, device: str) -> dict:
    """Return the current mode and allowed-VLAN list of *interface* on *device*.

    Keys:
        existed:       True / False / None  (None = could not determine)
        mode:          'access' / 'trunk' / 'unknown' / None
        allowed_vlans: list[int] / None
    """
    try:
        response = port_service.list_ports(device)
        match = next((p for p in response.ports if p.name == interface), None)
        if match is None:
            return {"existed": False, "mode": None, "allowed_vlans": None}
        return {
            "existed": True,
            "mode": match.mode,
            "allowed_vlans": match.allowed_vlans,
        }
    except Exception as exc:
        logger.warning(
            "Could not capture trunk-vlans pre-state for port %s on %s: %s",
            interface, device, exc,
        )
        return {"existed": None, "mode": None, "allowed_vlans": None}


def _get_trunk_vlans_pre_state(interface: str, device: str) -> dict:
    """Dispatch the trunk-vlans pre-state capture through ``app.api.ports``
    so tests can monkeypatch the single attachment point."""
    import app.api.ports as _ports_api  # lazy: avoids circular import at load time
    return _ports_api._capture_pre_state_port_trunk_vlans(interface, device)


def _compute_desired_vlans(
    mode: str,
    current_vlans: list[int] | None,
    requested_vlans: list[int],
) -> list[int] | None:
    """Compute the final VLAN list to send to the driver.

    Returns ``None`` when the current list is required but unavailable
    (add/remove with unknown pre-state).
    """
    if mode == "replace":
        return sorted(set(requested_vlans))
    if current_vlans is None:
        return None
    if mode == "add":
        return sorted(set(current_vlans) | set(requested_vlans))
    if mode == "remove":
        return sorted(set(current_vlans) - set(requested_vlans))
    return None


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
    from app.services.orchestration_runner import NoopOutcome, ValidationFailure, run_operation

    def _validate(ps: dict) -> ValidationFailure | None:
        if ps.get("existed") is None:
            msg = f"Cannot determine port state on device '{device}' — aborting operation"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "error": {"type": "precheck_failed", "message": msg},
                    "error_type": "permanent",
                },
                is_precheck=True,
            )
        if ps.get("existed") is False:
            msg = f"Interface '{interface}' does not exist on device '{device}'"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "validation": "failed",
                    "reason": "interface_not_found",
                    "error": {"type": "validation_error", "message": msg},
                },
            )
        return None

    def _noop(ps: dict) -> NoopOutcome | None:
        prev = (ps.get("description") or "").strip()
        desired = (description or "").strip()
        if prev == desired:
            return NoopOutcome(
                output="Description already matches requested value, no changes needed",
                message="Description unchanged (no changes needed)",
                audit_reason="description_unchanged_no_op",
            )
        return None

    def _success_log(duration: float, retry_count: int) -> None:
        logger.info(
            "Job %s: completed — description updated on %s/%s in %.2fs (retries=%d)",
            job_id, device, interface, duration, retry_count,
        )

    run_operation(
        job_id=job_id,
        device=device,
        audit_id=audit_id,
        operation_label=f"update description on interface={interface}",
        execute=lambda: port_service.update_port_description_on_device(interface, description, device),
        retry_base_delay=retry_base_delay,
        pre_state=pre_state,
        group_job_id=group_job_id,
        capture_pre_state=lambda: _get_pre_state(interface, device),
        validate_pre_state=_validate,
        check_noop=_noop,
        rollback=lambda ps: _rollback_description(interface, device, job_id, ps),
        logger=logger,
        success_log=_success_log,
    )


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
    from app.services.orchestration_runner import NoopOutcome, ValidationFailure, run_operation

    def _validate(ps: dict) -> ValidationFailure | None:
        if ps.get("existed") is None:
            msg = f"Cannot determine port state on device '{device}' — aborting operation"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "error": {"type": "precheck_failed", "message": msg},
                    "error_type": "permanent",
                },
                is_precheck=True,
            )
        if ps.get("existed") is False:
            msg = f"Interface '{interface}' does not exist on device '{device}'"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "validation": "failed",
                    "reason": "interface_not_found",
                    "error": {"type": "validation_error", "message": msg},
                },
            )
        return None

    def _noop(ps: dict) -> NoopOutcome | None:
        prev_admin = ps.get("admin_up")
        if prev_admin is not None and bool(prev_admin) == bool(enabled):
            return NoopOutcome(
                output="Admin state already matches requested value, no changes needed",
                message="Admin state unchanged (no changes needed)",
                audit_reason="admin_state_unchanged_no_op",
            )
        return None

    def _success_log(duration: float, retry_count: int) -> None:
        logger.info(
            "Job %s: completed — admin state on %s/%s set to %s in %.2fs (retries=%d)",
            job_id, device, interface,
            "enabled" if enabled else "disabled",
            duration, retry_count,
        )

    run_operation(
        job_id=job_id,
        device=device,
        audit_id=audit_id,
        operation_label=f"set admin state on interface={interface} enabled={enabled}",
        execute=lambda: port_service.set_port_admin_state_on_device(interface, enabled, device),
        retry_base_delay=retry_base_delay,
        pre_state=pre_state,
        group_job_id=group_job_id,
        capture_pre_state=lambda: _get_admin_pre_state(interface, device),
        validate_pre_state=_validate,
        check_noop=_noop,
        rollback=lambda ps: _rollback_admin_state(interface, device, job_id, ps),
        logger=logger,
        success_log=_success_log,
    )


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


# ── Step 2.3: set access VLAN ─────────────────────────────────────────────────

def _rollback_access_vlan(
    interface: str,
    device: str,
    job_id: str,
    pre_state: dict,
) -> tuple[bool, bool | None]:
    """Restore the prior access VLAN on *interface*.

    Returns ``(rollback_performed, rollback_success)`` using the same
    contract as the description and admin-state rollback helpers.
    """
    if pre_state.get("existed") is not True:
        logger.info(
            "Job %s rollback skipped — port %s did not exist before operation on device=%s",
            job_id, interface, device,
        )
        return False, None

    prev_vlan = pre_state.get("access_vlan")
    if prev_vlan is None:
        logger.info(
            "Job %s rollback skipped — prior access VLAN unknown for port %s on device=%s",
            job_id, interface, device,
        )
        return False, None

    logger.info(
        "Job %s rollback started for port %s on device=%s (restore access_vlan=%s)",
        job_id, interface, device, prev_vlan,
    )
    job_service.update_job(job_id, current_step="rollback_started")
    _rb_t0 = time.time()

    prev_mode = pre_state.get("mode")
    is_trunk = prev_mode == "trunk"

    try:
        if is_trunk:
            rb = port_service.set_trunk_pvid_vlan_on_device(interface, int(prev_vlan), device)
        else:
            rb = port_service.set_port_access_vlan_on_device(interface, int(prev_vlan), device)
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

    try:
        response = port_service.list_ports(device)
        match = next((p for p in response.ports if p.name == interface), None)
        current = match.access_vlan if match else None
        success = (current is not None) and (int(current) == int(prev_vlan))
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


def run_set_access_vlan_job(
    job_id: str,
    interface: str,
    vlan_id: int,
    device: str,
    audit_id: str,
    retry_base_delay: float = 1.0,
    pre_state: dict | None = None,
    group_job_id: str | None = None,
):
    """Execute one access-VLAN assignment.  Never raises — failures funnel
    into the job record / audit event."""
    from app.services.orchestration_runner import NoopOutcome, ValidationFailure, run_operation

    def _validate(ps: dict) -> ValidationFailure | None:
        if ps.get("existed") is None:
            msg = f"Cannot determine port state on device '{device}' — aborting operation"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "error": {"type": "precheck_failed", "message": msg},
                    "error_type": "permanent",
                },
                is_precheck=True,
            )
        if ps.get("existed") is False:
            msg = f"Interface '{interface}' does not exist on device '{device}'"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "validation": "failed",
                    "reason": "interface_not_found",
                    "error": {"type": "validation_error", "message": msg},
                },
            )
        current_mode = ps.get("mode")
        if current_mode is not None and current_mode not in ("access", "trunk"):
            msg = (
                f"Interface '{interface}' on device '{device}' is in "
                f"'{current_mode}' mode — PVID can only be set on access or trunk ports"
            )
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "validation": "failed",
                    "reason": "port_not_in_access_or_trunk_mode",
                    "error": {"type": "validation_error", "message": msg},
                },
            )
        return None

    def _noop(ps: dict) -> NoopOutcome | None:
        prev_vlan = ps.get("access_vlan")
        if prev_vlan is not None and int(prev_vlan) == int(vlan_id):
            return NoopOutcome(
                output="PVID already matches requested value, no changes needed",
                message="PVID unchanged (no changes needed)",
                audit_reason="access_vlan_unchanged_no_op",
            )
        return None

    def _execute(ps: dict) -> dict:
        # Captured-mode picks the playbook: trunk uses PVID; access uses
        # the regular access-VLAN flow.
        is_trunk = ps.get("mode") == "trunk"
        if is_trunk:
            return port_service.set_trunk_pvid_vlan_on_device(interface, vlan_id, device)
        return port_service.set_port_access_vlan_on_device(interface, vlan_id, device)

    def _success_log(duration: float, retry_count: int) -> None:
        logger.info(
            "Job %s: completed — access VLAN on %s/%s set to %d in %.2fs (retries=%d)",
            job_id, device, interface, vlan_id, duration, retry_count,
        )

    run_operation(
        job_id=job_id,
        device=device,
        audit_id=audit_id,
        operation_label=f"set access VLAN on interface={interface} vlan_id={vlan_id}",
        execute_with_pre_state=_execute,
        retry_base_delay=retry_base_delay,
        pre_state=pre_state,
        group_job_id=group_job_id,
        capture_pre_state=lambda: _get_access_vlan_pre_state(interface, device),
        validate_pre_state=_validate,
        check_noop=_noop,
        rollback=lambda ps: _rollback_access_vlan(interface, device, job_id, ps),
        logger=logger,
        success_log=_success_log,
    )


def enqueue_set_access_vlan_job(
    interface: str,
    vlan_id: int,
    device: str,
    username: str,
    background_tasks: BackgroundTasks,
    retry_base_delay: float = 1.0,
) -> tuple[list[dict], str]:
    """Create job + group-job records and schedule the access-VLAN runner."""
    from app.services import device_locks

    request_id = str(uuid.uuid4())
    group_job = group_job_service.create_group_job(
        operation="set_access_vlan",
        playbook="set_access_vlan.yml",
        parameters={"interface": interface, "vlan_id": vlan_id},
        devices=[device],
    )

    job = job_service.create_job(
        playbook="set_access_vlan.yml",
        device=device,
        parameters={"interface": interface, "vlan_id": vlan_id},
        group_job_id=group_job.group_job_id,
    )
    audit = audit_service.log_action(
        user=username,
        action="set_access_vlan",
        resource="port",
        details={"interface": interface, "vlan_id": vlan_id, "device": device},
        status="pending",
        job_id=job.job_id,
        device=device,
        request_id=request_id,
    )

    try:
        with device_locks.acquire(device, timeout=30):
            pre_state = _get_access_vlan_pre_state(interface, device)
    except TimeoutError:
        logger.warning(
            "Device %s busy during pre-state capture for port %s — job will abort on start",
            device, interface,
        )
        pre_state = {"existed": None, "mode": None, "access_vlan": None}

    background_tasks.add_task(
        run_set_access_vlan_job,
        job.job_id,
        interface,
        int(vlan_id),
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


# ── Step 2.4: set trunk allowed VLANs ────────────────────────────────────────

def _rollback_trunk_vlans(
    interface: str,
    device: str,
    job_id: str,
    pre_state: dict,
) -> tuple[bool, bool | None]:
    """Restore the prior allowed-VLAN list on *interface*.

    Returns ``(rollback_performed, rollback_success)`` using the same
    contract as the previous rollback helpers.
    """
    if pre_state.get("existed") is not True:
        logger.info(
            "Job %s rollback skipped — port %s did not exist before operation on device=%s",
            job_id, interface, device,
        )
        return False, None

    prev_vlans = pre_state.get("allowed_vlans")
    if prev_vlans is None:
        logger.info(
            "Job %s rollback skipped — prior allowed-VLAN list unknown for port %s on device=%s",
            job_id, interface, device,
        )
        return False, None

    logger.info(
        "Job %s rollback started for port %s on device=%s (restore %d VLANs)",
        job_id, interface, device, len(prev_vlans),
    )
    job_service.update_job(job_id, current_step="rollback_started")
    _rb_t0 = time.time()

    try:
        rb = port_service.set_trunk_allowed_vlans_on_device(interface, list(prev_vlans), device)
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

    try:
        response = port_service.list_ports(device)
        match = next((p for p in response.ports if p.name == interface), None)
        current = match.allowed_vlans if match else None
        success = (current is not None) and (sorted(current) == sorted(prev_vlans))
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


def run_set_trunk_allowed_vlans_job(
    job_id: str,
    interface: str,
    vlans: list[int],
    mode: str,
    device: str,
    audit_id: str,
    retry_base_delay: float = 1.0,
    pre_state: dict | None = None,
    group_job_id: str | None = None,
):
    """Execute one trunk allowed-VLAN assignment.  Never raises — failures
    funnel into the job record / audit event.

    The audit payload includes a ``desired_vlans`` key (the post-merge list
    that was actually sent to the driver); the runner's ``extra_audit`` hook
    threads it onto both success and failure events. ``desired`` is computed
    inside ``validate_pre_state`` against the captured ``allowed_vlans`` and
    cached in a shared scratch dict so the execute / extra_audit closures
    don't have to recompute it.
    """
    from app.services.orchestration_runner import NoopOutcome, ValidationFailure, run_operation

    _scratch: dict = {"desired": None}

    def _validate(ps: dict) -> ValidationFailure | None:
        if ps.get("existed") is None:
            msg = f"Cannot determine port state on device '{device}' — aborting operation"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "error": {"type": "precheck_failed", "message": msg},
                    "error_type": "permanent",
                },
                is_precheck=True,
            )
        if ps.get("existed") is False:
            msg = f"Interface '{interface}' does not exist on device '{device}'"
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "validation": "failed",
                    "reason": "interface_not_found",
                    "error": {"type": "validation_error", "message": msg},
                },
            )
        current_mode = ps.get("mode")
        if current_mode is not None and current_mode != "trunk":
            msg = (
                f"Interface '{interface}' on device '{device}' is in "
                f"'{current_mode}' mode — trunk VLAN management requires a trunk port"
            )
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "validation": "failed",
                    "reason": "port_not_in_trunk_mode",
                    "error": {"type": "validation_error", "message": msg},
                },
            )

        current_vlans = ps.get("allowed_vlans")
        desired = _compute_desired_vlans(mode, current_vlans, vlans)
        if desired is None:
            msg = (
                f"Cannot compute desired VLAN list: mode='{mode}' requires the "
                f"current allowed-VLAN list but it is unknown for port '{interface}' "
                f"on device '{device}'"
            )
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "validation": "failed",
                    "reason": "current_vlans_unknown",
                    "error": {"type": "validation_error", "message": msg},
                },
            )
        if len(desired) == 0:
            msg = (
                f"The requested remove operation would leave port '{interface}' "
                f"on device '{device}' with no allowed VLANs — rejected to prevent "
                "a complete trunk blackout"
            )
            return ValidationFailure(
                error_msg=msg,
                audit_extra={
                    "validation": "failed",
                    "reason": "remove_would_empty_trunk",
                    "error": {"type": "validation_error", "message": msg},
                },
            )
        _scratch["desired"] = desired
        return None

    def _noop(ps: dict) -> NoopOutcome | None:
        current_vlans = ps.get("allowed_vlans")
        desired = _scratch.get("desired")
        if current_vlans is not None and desired is not None and sorted(desired) == sorted(current_vlans):
            return NoopOutcome(
                output="Trunk allowed-VLAN list already matches requested value, no changes needed",
                message="Trunk VLANs unchanged (no changes needed)",
                audit_reason="trunk_vlans_unchanged_no_op",
            )
        return None

    def _execute(_ps: dict) -> dict:
        return port_service.set_trunk_allowed_vlans_on_device(interface, _scratch["desired"], device)

    def _success_log(duration: float, retry_count: int) -> None:
        logger.info(
            "Job %s: completed — trunk VLANs on %s/%s set (%d VLANs) in %.2fs (retries=%d)",
            job_id, device, interface, len(_scratch["desired"] or []), duration, retry_count,
        )

    # ``extra_audit`` is a callable so the runner re-reads ``_scratch``
    # after validate_pre_state has populated it.
    def _extras() -> dict:
        return {"desired_vlans": _scratch["desired"]} if _scratch.get("desired") is not None else {}

    run_operation(
        job_id=job_id,
        device=device,
        audit_id=audit_id,
        operation_label=f"set trunk VLANs on interface={interface} mode={mode}",
        execute_with_pre_state=_execute,
        retry_base_delay=retry_base_delay,
        pre_state=pre_state,
        group_job_id=group_job_id,
        capture_pre_state=lambda: _get_trunk_vlans_pre_state(interface, device),
        validate_pre_state=_validate,
        check_noop=_noop,
        rollback=lambda ps: _rollback_trunk_vlans(interface, device, job_id, ps),
        extra_audit=_extras,
        logger=logger,
        success_log=_success_log,
    )


def enqueue_set_trunk_allowed_vlans_job(
    interface: str,
    vlans: list[int],
    mode: str,
    device: str,
    username: str,
    background_tasks: BackgroundTasks,
    retry_base_delay: float = 1.0,
) -> tuple[list[dict], str]:
    """Create job + group-job records and schedule the trunk-VLAN runner."""
    from app.services import device_locks

    request_id = str(uuid.uuid4())
    group_job = group_job_service.create_group_job(
        operation="set_trunk_allowed_vlans",
        playbook="set_trunk_allowed_vlans.yml",
        parameters={"interface": interface, "vlans": vlans, "mode": mode},
        devices=[device],
    )

    job = job_service.create_job(
        playbook="set_trunk_allowed_vlans.yml",
        device=device,
        parameters={"interface": interface, "vlans": vlans, "mode": mode},
        group_job_id=group_job.group_job_id,
    )
    audit = audit_service.log_action(
        user=username,
        action="set_trunk_allowed_vlans",
        resource="port",
        details={"interface": interface, "vlans": vlans, "mode": mode, "device": device},
        status="pending",
        job_id=job.job_id,
        device=device,
        request_id=request_id,
    )

    try:
        with device_locks.acquire(device, timeout=30):
            pre_state = _get_trunk_vlans_pre_state(interface, device)
    except TimeoutError:
        logger.warning(
            "Device %s busy during pre-state capture for port %s — job will abort on start",
            device, interface,
        )
        pre_state = {"existed": None, "mode": None, "allowed_vlans": None}

    background_tasks.add_task(
        run_set_trunk_allowed_vlans_job,
        job.job_id,
        interface,
        list(vlans),
        mode,
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
