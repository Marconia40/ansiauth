"""Tests for DeviceSyncService.sync_core() -- Decision 3 orchestration.

The wire-level win of Decision 3 (fewer SSH sessions) is verified in
test_read_core_state.py. This file verifies the *orchestration* piece:
the sync service must take the per-device Redis lock exactly ONCE
(instead of three times, once per sync_vlans/ports/svis), and must
still honour the partial-failure policy on persist errors.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.services.device_sync_service import DeviceSyncService


class _FakeCoordinator:
    """RedisCoordinator stub -- counts lock acquisitions so tests can
    assert sync_core takes it exactly once."""
    def __init__(self):
        self.lock_calls: list[str] = []
        self.marked_ok: list[tuple[str, str, str]] = []
        self.marked_err: list[tuple[str, str, str]] = []

    @contextmanager
    def bloquear(self, device_id: str, timeout=None):
        self.lock_calls.append(device_id)
        yield


class _FakeRepo:
    """Bare-minimum repo used for the 3 core resource tables."""
    def __init__(self):
        self._items: dict = {}
        self.add_calls: list = []
        self.remove_calls: list = []

    def list(self, device=None):
        return list(self._items.values())

    def add(self, obj):
        self.add_calls.append(obj)

    def remove(self, key):
        self.remove_calls.append(key)


class _StubDevice:
    def __init__(self, name="test-dev"):
        self.name = name
        self.password = "pw"
        self.driver = MagicMock()


def _make_service():
    coord = _FakeCoordinator()
    svc = DeviceSyncService(
        vlan_repo=_FakeRepo(),
        puerto_repo=_FakeRepo(),
        svi_repo=_FakeRepo(),
        global_config_repo=_FakeRepo(),
        coordinator=coord,
        arp_mac_repo=_FakeRepo(),
        device_logs_repo=_FakeRepo(),
    )
    return svc, coord


def test_sync_core_takes_lock_exactly_once():
    """The whole point of sync_core: 1 lock acquisition + 1 driver read
    for the combined refresh, not 3."""
    svc, coord = _make_service()
    dev = _StubDevice()
    dev.driver.read_core_state.return_value = ([], [], [])

    # Bypass DB writes -- _marcar_ok/_marcar_error hit the real ORM.
    with patch.object(svc, "_marcar_ok"), patch.object(svc, "_marcar_error"):
        svc.sync_core(dev)

    assert coord.lock_calls == ["test-dev"], (
        f"Expected 1 lock acquisition on 'test-dev', saw {coord.lock_calls}"
    )
    dev.driver.read_core_state.assert_called_once_with(dev, "pw")


def test_sync_core_read_failure_marks_all_three_scopes():
    """If the driver read fails, all 3 *_sync_error fields get the same
    message and the lock is still released cleanly."""
    svc, coord = _make_service()
    dev = _StubDevice()
    dev.driver.read_core_state.side_effect = RuntimeError("SSH boom")

    marked: list[tuple[str, str]] = []

    def capture_err(device_name, field, msg):
        marked.append((field, msg))

    with patch.object(svc, "_marcar_error", side_effect=capture_err), \
         patch.object(svc, "_marcar_ok"):
        with pytest.raises(RuntimeError, match="SSH boom"):
            svc.sync_core(dev)

    fields = [f for (f, _) in marked]
    assert "vlans_sync_error" in fields
    assert "ports_sync_error" in fields
    assert "svis_sync_error" in fields
    # Lock was taken and released -- no leaks.
    assert coord.lock_calls == ["test-dev"]


def test_sync_core_partial_persist_failure_preserves_the_others():
    """If the read succeeds but one scope's persist blows up, the other
    two are still persisted + marked OK, the failing one gets its
    sync_error set, and the method re-raises so Celery observes it."""
    svc, coord = _make_service()
    dev = _StubDevice()
    dev.driver.read_core_state.return_value = ([], [], [])

    # Make _persistir_ports blow up; vlans + svis should still go OK.
    with patch.object(svc, "_persistir_vlans", return_value=0), \
         patch.object(svc, "_persistir_ports", side_effect=ValueError("ports parse fail")), \
         patch.object(svc, "_persistir_svis", return_value=0), \
         patch.object(svc, "_marcar_ok") as mock_ok, \
         patch.object(svc, "_marcar_error") as mock_err:
        with pytest.raises(RuntimeError, match="partial failure"):
            svc.sync_core(dev)

    ok_fields = {call.args[1] for call in mock_ok.call_args_list}
    err_fields = {call.args[1] for call in mock_err.call_args_list}
    # vlans and svis: marked OK (persistir succeeded)
    assert "vlans_synced_at" in ok_fields
    assert "svis_synced_at" in ok_fields
    # ports: only error, no OK
    assert "ports_synced_at" not in ok_fields
    assert "ports_sync_error" in err_fields
