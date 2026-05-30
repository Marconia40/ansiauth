"""Tests for port_config_service (Step 3.3).

Covers:
* Mock mode: configure_port, shutdown_port, enable_port reach completed
* Huawei driver success path (driver method called via dispatcher)
* Failure mapping (job status=failed, error captured, audit event written)
* Lock behavior (device_locks.acquire called during execution)
* Dispatcher usage (vendor method resolved correctly)
* Retry compatibility (transient failure increments retry_count)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.main import app
from app.models.device import Device
from app.models.port import PortConfigRequest, PortConfigResult, PortInfo, PortListResponse
from app.services import job_service, port_config_service, port_service


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client(role: str) -> TestClient:
    token = create_access_token({"sub": role, "role": role})
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})
    return c


_INTERFACE = "GigabitEthernet0/0/1"
_DEVICE = "mock_device"


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "mock")


@pytest.fixture
def mock_device(monkeypatch):
    dev = Device(
        name=_DEVICE,
        host="192.0.2.1",
        vendor="huawei_vrp",
        username="admin",
        encrypted_password="encrypted",
        platform="vrp",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: dev if name == _DEVICE else None,
    )
    return dev


def _stub_pre_state_configure(monkeypatch, state: dict | None = None):
    """Monkeypatch the configure pre-state hook to return a fixed value."""
    pre = state or {
        "existed": True,
        "description": "old desc",
        "admin_up": True,
        "mode": "access",
        "access_vlan": 10,
        "allowed_vlans": None,
    }
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_configure",
        lambda interface, device: pre,
    )
    return pre


def _stub_pre_state_admin(monkeypatch, admin_up: bool = True):
    """Monkeypatch shutdown/enable pre-state hooks."""
    pre = {"existed": True, "admin_up": admin_up}
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_shutdown",
        lambda interface, device: pre,
    )
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_enable",
        lambda interface, device: pre,
    )
    return pre


# ── Mock-mode tests ───────────────────────────────────────────────────────────

def test_configure_port_mock_mode_success(mock_mode, mock_device, monkeypatch):
    """configure_port in mock mode creates a job that reaches completed."""
    _stub_pre_state_configure(monkeypatch)
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "new desc"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    job_id = body["jobs"][0]["job_id"]

    time.sleep(0.05)
    job = job_service.get_job(job_id)
    assert job.status == "completed"


def test_shutdown_port_mock_mode_success(mock_mode, mock_device, monkeypatch):
    """shutdown_port in mock mode creates a job that reaches completed."""
    _stub_pre_state_admin(monkeypatch, admin_up=True)
    res = _client("operator").post(
        "/api/v1/ports/shutdown",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    job_id = body["jobs"][0]["job_id"]

    time.sleep(0.05)
    job = job_service.get_job(job_id)
    assert job.status == "completed"


def test_enable_port_mock_mode_success(mock_mode, mock_device, monkeypatch):
    """enable_port in mock mode creates a job that reaches completed."""
    _stub_pre_state_admin(monkeypatch, admin_up=False)
    res = _client("operator").post(
        "/api/v1/ports/enable",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["success"] is True
    job_id = body["jobs"][0]["job_id"]

    time.sleep(0.05)
    job = job_service.get_job(job_id)
    assert job.status == "completed"


# ── Huawei driver success path ────────────────────────────────────────────────

def test_configure_port_huawei_success(monkeypatch):
    """configure_port dispatches to the vendor driver and completes."""
    dev = Device(
        name=_DEVICE, host="192.0.2.1", vendor="huawei_vrp",
        username="admin", encrypted_password="enc", platform="vrp",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: dev if name == _DEVICE else None,
    )
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "real")

    mock_driver = MagicMock()
    mock_driver.configure_port.return_value = PortConfigResult(
        success=True, changed=True, interface=_INTERFACE, vendor="huawei_vrp",
        execution_time_ms=50.0,
    )
    monkeypatch.setattr(
        "app.services.vendors.dispatcher.get_port_driver",
        lambda device: mock_driver,
    )
    monkeypatch.setattr(
        "app.services.secret_service.decrypt_password",
        lambda enc: "plaintext",
    )
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_configure",
        lambda i, d: {
            "existed": True, "description": "old", "admin_up": True,
            "mode": "access", "access_vlan": 5, "allowed_vlans": None,
        },
    )

    config = PortConfigRequest(device=_DEVICE, interface=_INTERFACE, description="new")
    pre = {
        "existed": True, "description": "old", "admin_up": True,
        "mode": "access", "access_vlan": 5, "allowed_vlans": None,
    }
    job = job_service.create_job(
        playbook="configure_port.yml",
        device=_DEVICE,
        parameters={"interface": _INTERFACE, "fields": ["description"]},
    )
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="configure_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_configure_port_job(
        job_id=job.job_id,
        config=config,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    assert job_service.get_job(job.job_id).status == "completed"
    mock_driver.configure_port.assert_called_once()


def test_shutdown_port_huawei_success(monkeypatch):
    """shutdown_port dispatches to the vendor driver and completes."""
    dev = Device(
        name=_DEVICE, host="192.0.2.1", vendor="huawei_vrp",
        username="admin", encrypted_password="enc", platform="vrp",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: dev if name == _DEVICE else None,
    )
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "real")

    mock_driver = MagicMock()
    mock_driver.shutdown_port.return_value = {"rc": 0, "stdout": "ok", "stderr": "", "success": True}
    monkeypatch.setattr(
        "app.services.vendors.dispatcher.get_port_driver",
        lambda device: mock_driver,
    )
    monkeypatch.setattr(
        "app.services.secret_service.decrypt_password",
        lambda enc: "plaintext",
    )

    pre = {"existed": True, "admin_up": True}
    job = job_service.create_job(
        playbook="shutdown_port.yml",
        device=_DEVICE,
        parameters={"interface": _INTERFACE},
    )
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="shutdown_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_shutdown_port_job(
        job_id=job.job_id,
        interface=_INTERFACE,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    assert job_service.get_job(job.job_id).status == "completed"
    mock_driver.shutdown_port.assert_called_once()


def test_enable_port_huawei_success(monkeypatch):
    """enable_port dispatches to the vendor driver and completes."""
    dev = Device(
        name=_DEVICE, host="192.0.2.1", vendor="huawei_vrp",
        username="admin", encrypted_password="enc", platform="vrp",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: dev if name == _DEVICE else None,
    )
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "real")

    mock_driver = MagicMock()
    mock_driver.enable_port.return_value = {"rc": 0, "stdout": "ok", "stderr": "", "success": True}
    monkeypatch.setattr(
        "app.services.vendors.dispatcher.get_port_driver",
        lambda device: mock_driver,
    )
    monkeypatch.setattr(
        "app.services.secret_service.decrypt_password",
        lambda enc: "plaintext",
    )

    pre = {"existed": True, "admin_up": False}
    job = job_service.create_job(
        playbook="enable_port.yml",
        device=_DEVICE,
        parameters={"interface": _INTERFACE},
    )
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="enable_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_enable_port_job(
        job_id=job.job_id,
        interface=_INTERFACE,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    assert job_service.get_job(job.job_id).status == "completed"
    mock_driver.enable_port.assert_called_once()


# ── Failure mapping ───────────────────────────────────────────────────────────

def test_configure_port_failure_maps_to_job_failed(monkeypatch, mock_mode, mock_device):
    """When the driver returns rc=1, the job status becomes failed."""
    _stub_pre_state_configure(monkeypatch)
    monkeypatch.setattr(
        port_service, "configure_port_on_device",
        lambda config, device_id: PortConfigResult(
            success=False, changed=False, interface=config.interface, vendor="mock"
        ),
    )

    config = PortConfigRequest(device=_DEVICE, interface=_INTERFACE, description="x")
    pre = {
        "existed": True, "description": "old", "admin_up": True,
        "mode": "access", "access_vlan": 5, "allowed_vlans": None,
    }
    job = job_service.create_job("configure_port.yml", _DEVICE, {"interface": _INTERFACE, "fields": ["description"]})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="configure_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_configure_port_job(
        job_id=job.job_id,
        config=config,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    final = job_service.get_job(job.job_id)
    assert final.status == "failed"
    assert final.error is not None


def test_shutdown_port_failure_maps_to_job_failed(monkeypatch, mock_mode, mock_device):
    """When shutdown_port_on_device returns rc=1, the job becomes failed."""
    monkeypatch.setattr(
        port_service, "shutdown_port_on_device",
        lambda interface, device_id: {"rc": 1, "stdout": "", "stderr": "Device error", "success": False},
    )
    monkeypatch.setattr(
        port_service, "enable_port_on_device",
        lambda interface, device_id: {"rc": 0, "stdout": "rollback ok", "stderr": "", "success": True},
    )

    pre = {"existed": True, "admin_up": True}
    job = job_service.create_job("shutdown_port.yml", _DEVICE, {"interface": _INTERFACE})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="shutdown_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_shutdown_port_job(
        job_id=job.job_id,
        interface=_INTERFACE,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    final = job_service.get_job(job.job_id)
    assert final.status == "failed"
    assert final.rollback_performed is True


def test_enable_port_failure_maps_to_job_failed(monkeypatch, mock_mode, mock_device):
    """When enable_port_on_device returns rc=1, the job becomes failed."""
    monkeypatch.setattr(
        port_service, "enable_port_on_device",
        lambda interface, device_id: {"rc": 1, "stdout": "", "stderr": "Device error", "success": False},
    )
    monkeypatch.setattr(
        port_service, "shutdown_port_on_device",
        lambda interface, device_id: {"rc": 0, "stdout": "rollback ok", "stderr": "", "success": True},
    )

    pre = {"existed": True, "admin_up": False}
    job = job_service.create_job("enable_port.yml", _DEVICE, {"interface": _INTERFACE})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="enable_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_enable_port_job(
        job_id=job.job_id,
        interface=_INTERFACE,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    final = job_service.get_job(job.job_id)
    assert final.status == "failed"
    assert final.rollback_performed is True


# ── Lock behavior ─────────────────────────────────────────────────────────────

def test_configure_port_acquires_device_lock(monkeypatch, mock_mode, mock_device):
    """The job runner acquires the device lock before executing."""
    _stub_pre_state_configure(monkeypatch)
    lock_acquired = []

    original_acquire = __import__(
        "app.services.device_locks", fromlist=["acquire"]
    ).acquire

    class _TrackingAcquire:
        def __init__(self, device, timeout=None):
            self._inner = original_acquire(device, timeout=timeout)

        def __enter__(self):
            lock_acquired.append(True)
            return self._inner.__enter__()

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

    monkeypatch.setattr(
        "app.services.device_locks.acquire",
        lambda device, timeout=None: _TrackingAcquire(device, timeout=timeout),
    )

    config = PortConfigRequest(device=_DEVICE, interface=_INTERFACE, description="test lock")
    pre = {
        "existed": True, "description": "old", "admin_up": True,
        "mode": "access", "access_vlan": 10, "allowed_vlans": None,
    }
    job = job_service.create_job("configure_port.yml", _DEVICE, {"interface": _INTERFACE, "fields": ["description"]})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="configure_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_configure_port_job(
        job_id=job.job_id,
        config=config,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    assert lock_acquired, "Device lock was not acquired during configure_port execution"


def test_shutdown_port_acquires_device_lock(monkeypatch, mock_mode, mock_device):
    """shutdown_port job runner acquires the device lock."""
    lock_acquired = []

    original_acquire = __import__(
        "app.services.device_locks", fromlist=["acquire"]
    ).acquire

    class _TrackingAcquire:
        def __init__(self, device, timeout=None):
            self._inner = original_acquire(device, timeout=timeout)

        def __enter__(self):
            lock_acquired.append(True)
            return self._inner.__enter__()

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

    monkeypatch.setattr(
        "app.services.device_locks.acquire",
        lambda device, timeout=None: _TrackingAcquire(device, timeout=timeout),
    )

    pre = {"existed": True, "admin_up": True}
    job = job_service.create_job("shutdown_port.yml", _DEVICE, {"interface": _INTERFACE})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="shutdown_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_shutdown_port_job(
        job_id=job.job_id,
        interface=_INTERFACE,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    assert lock_acquired, "Device lock was not acquired during shutdown_port execution"


# ── Dispatcher usage ──────────────────────────────────────────────────────────

def test_configure_port_uses_dispatcher(monkeypatch, mock_mode, mock_device):
    """configure_port_on_device resolves the driver via the dispatcher."""
    _stub_pre_state_configure(monkeypatch)
    dispatch_calls = []

    original_get_driver = port_service._get_driver

    def _spy_get_driver(device):
        dispatch_calls.append(device)
        return original_get_driver(device)

    # We run via mock mode (no real driver call), but patch configure_port_on_device
    # to verify the dispatcher pattern by patching _get_driver at the port_service level.
    monkeypatch.setattr(port_service, "_get_driver", _spy_get_driver)
    monkeypatch.setattr(port_service, "EXECUTION_MODE", "real")

    mock_driver = MagicMock()
    mock_driver.configure_port.return_value = PortConfigResult(
        success=True, changed=True, interface=_INTERFACE, vendor="huawei_vrp",
    )
    monkeypatch.setattr(
        "app.services.vendors.dispatcher.get_port_driver",
        lambda device: mock_driver,
    )
    monkeypatch.setattr("app.services.secret_service.decrypt_password", lambda e: "pw")

    dev = Device(
        name=_DEVICE, host="192.0.2.1", vendor="huawei_vrp",
        username="admin", encrypted_password="enc", platform="vrp",
    )
    monkeypatch.setattr(
        "app.services.device_service.get_device",
        lambda name: dev if name == _DEVICE else None,
    )

    config = PortConfigRequest(device=_DEVICE, interface=_INTERFACE, description="via dispatch")
    pre = {
        "existed": True, "description": "old", "admin_up": True,
        "mode": "access", "access_vlan": 5, "allowed_vlans": None,
    }
    job = job_service.create_job("configure_port.yml", _DEVICE, {"interface": _INTERFACE, "fields": ["description"]})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="configure_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_configure_port_job(
        job_id=job.job_id,
        config=config,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    assert dispatch_calls, "port_service._get_driver was never called — dispatcher bypassed"
    assert job_service.get_job(job.job_id).status == "completed"


# ── Retry compatibility ───────────────────────────────────────────────────────

def test_configure_port_retries_on_transient_failure(monkeypatch, mock_mode, mock_device):
    """A transient failure (connection error) causes configure_port to retry."""
    _stub_pre_state_configure(monkeypatch)

    call_count = [0]

    def _flaky_configure(config, device_id):
        call_count[0] += 1
        if call_count[0] < 2:
            return PortConfigResult(
                success=False, changed=False, interface=config.interface, vendor="mock"
            )
        return PortConfigResult(
            success=True, changed=True, interface=config.interface, vendor="mock",
        )

    # Make the failure look transient (connection-related message)
    def _flaky_configure_with_stderr(config, device_id):
        call_count[0] += 1
        if call_count[0] < 2:
            # Return a dict-like PortConfigResult that triggers transient retry
            raise RuntimeError("Connection timed out")
        return PortConfigResult(
            success=True, changed=True, interface=config.interface, vendor="mock",
        )

    monkeypatch.setattr(port_service, "configure_port_on_device", _flaky_configure_with_stderr)

    config = PortConfigRequest(device=_DEVICE, interface=_INTERFACE, description="retry test")
    pre = {
        "existed": True, "description": "old", "admin_up": True,
        "mode": "access", "access_vlan": 10, "allowed_vlans": None,
    }
    job = job_service.create_job("configure_port.yml", _DEVICE, {"interface": _INTERFACE, "fields": ["description"]})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="configure_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_configure_port_job(
        job_id=job.job_id,
        config=config,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    final = job_service.get_job(job.job_id)
    assert final.status == "completed"
    assert call_count[0] == 2, f"Expected 2 calls (1 failure + 1 success), got {call_count[0]}"
    assert final.retry_count >= 1


def test_shutdown_port_retries_on_transient_failure(monkeypatch, mock_mode, mock_device):
    """A transient failure causes shutdown_port to retry and then complete."""
    call_count = [0]

    def _flaky_shutdown(interface, device_id):
        call_count[0] += 1
        if call_count[0] < 2:
            raise RuntimeError("Connection timed out")
        return {"rc": 0, "stdout": "ok", "stderr": "", "success": True}

    monkeypatch.setattr(port_service, "shutdown_port_on_device", _flaky_shutdown)

    pre = {"existed": True, "admin_up": True}
    job = job_service.create_job("shutdown_port.yml", _DEVICE, {"interface": _INTERFACE})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="shutdown_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_shutdown_port_job(
        job_id=job.job_id,
        interface=_INTERFACE,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    final = job_service.get_job(job.job_id)
    assert final.status == "completed"
    assert call_count[0] == 2
    assert final.retry_count >= 1


def test_enable_port_retries_on_transient_failure(monkeypatch, mock_mode, mock_device):
    """A transient failure causes enable_port to retry and then complete."""
    call_count = [0]

    def _flaky_enable(interface, device_id):
        call_count[0] += 1
        if call_count[0] < 2:
            raise RuntimeError("Connection timed out")
        return {"rc": 0, "stdout": "ok", "stderr": "", "success": True}

    monkeypatch.setattr(port_service, "enable_port_on_device", _flaky_enable)

    pre = {"existed": True, "admin_up": False}
    job = job_service.create_job("enable_port.yml", _DEVICE, {"interface": _INTERFACE})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="enable_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_enable_port_job(
        job_id=job.job_id,
        interface=_INTERFACE,
        device=_DEVICE,
        audit_id=audit.id,
        retry_base_delay=0.001,
        pre_state=pre,
    )

    final = job_service.get_job(job.job_id)
    assert final.status == "completed"
    assert call_count[0] == 2
    assert final.retry_count >= 1


# ── Interface not found ───────────────────────────────────────────────────────

def test_configure_port_interface_not_found(monkeypatch, mock_mode, mock_device):
    """When the interface does not exist, the job fails with validation error."""
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_configure",
        lambda i, d: {
            "existed": False, "description": None, "admin_up": None,
            "mode": None, "access_vlan": None, "allowed_vlans": None,
        },
    )

    config = PortConfigRequest(device=_DEVICE, interface="GigabitEthernet9/9/9", description="x")
    pre = {
        "existed": False, "description": None, "admin_up": None,
        "mode": None, "access_vlan": None, "allowed_vlans": None,
    }
    job = job_service.create_job("configure_port.yml", _DEVICE, {"interface": "GigabitEthernet9/9/9", "fields": ["description"]})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="configure_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_configure_port_job(
        job_id=job.job_id, config=config, device=_DEVICE,
        audit_id=audit.id, retry_base_delay=0.001, pre_state=pre,
    )

    final = job_service.get_job(job.job_id)
    assert final.status == "failed"
    assert "does not exist" in (final.error or "")


# ── Idempotency ───────────────────────────────────────────────────────────────

def test_configure_port_noop_when_already_matches(monkeypatch, mock_mode, mock_device):
    """configure_port completes as no-op when all requested fields already match."""
    pre = {
        "existed": True, "description": "same desc", "admin_up": True,
        "mode": "access", "access_vlan": 10, "allowed_vlans": None,
    }
    monkeypatch.setattr(
        "app.api.ports._capture_pre_state_port_configure",
        lambda i, d: pre,
    )

    config = PortConfigRequest(device=_DEVICE, interface=_INTERFACE, description="same desc")
    job = job_service.create_job("configure_port.yml", _DEVICE, {"interface": _INTERFACE, "fields": ["description"]})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="configure_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_configure_port_job(
        job_id=job.job_id, config=config, device=_DEVICE,
        audit_id=audit.id, retry_base_delay=0.001, pre_state=pre,
    )

    final = job_service.get_job(job.job_id)
    assert final.status == "completed"
    assert final.result is not None
    assert final.result.get("operation_result") == "noop"


def test_shutdown_port_noop_when_already_down(monkeypatch, mock_mode, mock_device):
    """shutdown_port completes as no-op when port is already administratively down."""
    pre = {"existed": True, "admin_up": False}
    job = job_service.create_job("shutdown_port.yml", _DEVICE, {"interface": _INTERFACE})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="shutdown_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_shutdown_port_job(
        job_id=job.job_id, interface=_INTERFACE, device=_DEVICE,
        audit_id=audit.id, retry_base_delay=0.001, pre_state=pre,
    )

    final = job_service.get_job(job.job_id)
    assert final.status == "completed"
    assert final.result is not None
    assert final.result.get("operation_result") == "noop"


def test_enable_port_noop_when_already_up(monkeypatch, mock_mode, mock_device):
    """enable_port completes as no-op when port is already administratively up."""
    pre = {"existed": True, "admin_up": True}
    job = job_service.create_job("enable_port.yml", _DEVICE, {"interface": _INTERFACE})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="enable_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_enable_port_job(
        job_id=job.job_id, interface=_INTERFACE, device=_DEVICE,
        audit_id=audit.id, retry_base_delay=0.001, pre_state=pre,
    )

    final = job_service.get_job(job.job_id)
    assert final.status == "completed"
    assert final.result is not None
    assert final.result.get("operation_result") == "noop"


# ── API validation ────────────────────────────────────────────────────────────

def test_configure_port_rejects_unauthenticated():
    res = TestClient(app).post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "x"},
    )
    assert res.status_code == 401


def test_shutdown_port_rejects_unauthenticated():
    res = TestClient(app).post(
        "/api/v1/ports/shutdown",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 401


def test_enable_port_rejects_unauthenticated():
    res = TestClient(app).post(
        "/api/v1/ports/enable",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 401


def test_configure_port_rejects_observer(mock_mode, mock_device):
    res = _client("observer").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE, "description": "x"},
    )
    assert res.status_code == 403


def test_configure_port_rejects_empty_mutation(mock_mode, mock_device):
    """A configure request with no mutation fields returns 422."""
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": _DEVICE, "interface": _INTERFACE},
    )
    assert res.status_code == 422


def test_configure_port_rejects_unknown_device(mock_mode, monkeypatch):
    """Unknown device returns 404."""
    monkeypatch.setattr("app.services.device_service.get_device", lambda name: None)
    res = _client("operator").post(
        "/api/v1/ports/configure",
        json={"device": "nonexistent", "interface": _INTERFACE, "description": "x"},
    )
    assert res.status_code == 404


def test_shutdown_port_rejects_unknown_device(mock_mode, monkeypatch):
    monkeypatch.setattr("app.services.device_service.get_device", lambda name: None)
    res = _client("operator").post(
        "/api/v1/ports/shutdown",
        json={"device": "nonexistent", "interface": _INTERFACE},
    )
    assert res.status_code == 404


def test_enable_port_rejects_unknown_device(mock_mode, monkeypatch):
    monkeypatch.setattr("app.services.device_service.get_device", lambda name: None)
    res = _client("operator").post(
        "/api/v1/ports/enable",
        json={"device": "nonexistent", "interface": _INTERFACE},
    )
    assert res.status_code == 404


# ── Rollback paths ────────────────────────────────────────────────────────────

def test_shutdown_port_no_rollback_when_was_already_down(monkeypatch, mock_mode, mock_device):
    """If the port was already down, shutdown failure triggers no rollback."""
    monkeypatch.setattr(
        port_service, "shutdown_port_on_device",
        lambda i, d: {"rc": 1, "stdout": "", "stderr": "err", "success": False},
    )
    rb_called = []
    monkeypatch.setattr(
        port_service, "enable_port_on_device",
        lambda i, d: rb_called.append(True) or {"rc": 0, "stdout": "", "stderr": "", "success": True},
    )

    pre = {"existed": True, "admin_up": False}
    job = job_service.create_job("shutdown_port.yml", _DEVICE, {"interface": _INTERFACE})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="shutdown_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    # This will hit the no-op branch (already down) before executing
    port_config_service.run_shutdown_port_job(
        job_id=job.job_id, interface=_INTERFACE, device=_DEVICE,
        audit_id=audit.id, retry_base_delay=0.001, pre_state=pre,
    )

    # Should be noop-completed, not failed (already down → idempotent no-op)
    final = job_service.get_job(job.job_id)
    assert final.status == "completed"
    assert not rb_called


def test_enable_rollback_calls_shutdown_when_was_down(monkeypatch, mock_mode, mock_device):
    """When enable_port fails and port was previously down, rollback calls shutdown_port."""
    monkeypatch.setattr(
        port_service, "enable_port_on_device",
        lambda i, d: {"rc": 1, "stdout": "", "stderr": "err", "success": False},
    )
    rb_called = []
    monkeypatch.setattr(
        port_service, "shutdown_port_on_device",
        lambda i, d: rb_called.append(True) or {"rc": 0, "stdout": "", "stderr": "", "success": True},
    )

    pre = {"existed": True, "admin_up": False}
    job = job_service.create_job("enable_port.yml", _DEVICE, {"interface": _INTERFACE})
    from app.services import audit_service
    audit = audit_service.log_action(
        user="test", action="enable_port", resource="port",
        details={}, status="pending", job_id=job.job_id, device=_DEVICE,
    )

    port_config_service.run_enable_port_job(
        job_id=job.job_id, interface=_INTERFACE, device=_DEVICE,
        audit_id=audit.id, retry_base_delay=0.001, pre_state=pre,
    )

    final = job_service.get_job(job.job_id)
    assert final.status == "failed"
    assert final.rollback_performed is True
    assert rb_called, "shutdown_port_on_device was not called during enable rollback"
