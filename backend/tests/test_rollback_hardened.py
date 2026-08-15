"""Tests for hardened rollback logic (step 3.3).

Covers: rollback_success field, post-rollback verification, pre-state guards,
and structured log messages — all via the private helpers and the HTTP API.
"""
import logging
import time

import pytest

from app.models.vlan import VLANInfo
from app.services import audit_service, vlan_service
import app.services.vlan_execution_service as svc


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_audit():
    audit_service.clear_audit_log()
    yield
    audit_service.clear_audit_log()


# ── _rollback_create unit tests ───────────────────────────────────────────────

class TestRollbackCreate:
    def _pre(self, existed):
        if existed is None:
            return {"existed": None, "vlan_data": None}
        if not existed:
            return {"existed": False, "vlan_data": None}
        return {"existed": True, "vlan_data": {"vlan_id": 10, "name": "MGMT"}}

    def test_no_rollback_when_vlan_existed(self, monkeypatch):
        """If VLAN already existed before create, rollback must NOT run."""
        deleted = {"n": 0}
        monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: deleted.__setitem__("n", deleted["n"] + 1) or {"rc": 0})
        performed, success = svc._rollback_create(10, "dev", "job1", self._pre(True))
        assert performed is False
        assert success is None
        assert deleted["n"] == 0

    def test_no_rollback_when_pre_state_unknown(self, monkeypatch):
        """If pre-state is unknown (existed=None), rollback must NOT run."""
        deleted = {"n": 0}
        monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: deleted.__setitem__("n", deleted["n"] + 1) or {"rc": 0})
        performed, success = svc._rollback_create(10, "dev", "job1", self._pre(None))
        assert performed is False
        assert success is None
        assert deleted["n"] == 0

    def test_rollback_success_when_vlan_absent_after_delete(self, monkeypatch):
        """Rollback succeeds if VLAN is gone after delete and verification confirms absence."""
        monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 0})
        monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: [])  # VLAN absent → good
        performed, success = svc._rollback_create(901, "dev", "job1", self._pre(False))
        assert performed is True
        assert success is True

    def test_rollback_fails_when_vlan_still_present_after_delete(self, monkeypatch):
        """Verification: if VLAN is still present after delete, rollback_success=False."""
        monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 0})
        monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: [VLANInfo(vlan_id=901, name="X")])
        performed, success = svc._rollback_create(901, "dev", "job1", self._pre(False))
        assert performed is True
        assert success is False

    def test_rollback_performed_true_but_fails_when_delete_nonzero(self, monkeypatch):
        """If the rollback delete command fails (rc!=0), performed=True, success=False."""
        monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 1, "stdout": "", "stderr": "err"})
        performed, success = svc._rollback_create(901, "dev", "job1", self._pre(False))
        assert performed is True
        assert success is False

    def test_rollback_performed_true_but_fails_when_delete_raises(self, monkeypatch):
        """If delete_vlan raises an exception, performed=True (attempted), success=False."""
        monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: (_ for _ in ()).throw(RuntimeError("SSH down")))
        performed, success = svc._rollback_create(901, "dev", "job1", self._pre(False))
        assert performed is True
        assert success is False

    def test_rollback_failed_when_get_vlans_raises(self, monkeypatch):
        """If get_vlans raises during verification, rollback_success=False."""
        monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 0})
        monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: (_ for _ in ()).throw(RuntimeError("read error")))
        performed, success = svc._rollback_create(901, "dev", "job1", self._pre(False))
        assert performed is True
        assert success is False


class TestRollbackDelete:
    def _pre(self, existed):
        if not existed:
            return {"existed": False, "vlan_data": None}
        return {"existed": True, "vlan_data": {"vlan_id": 10, "name": "MGMT"}}

    def test_no_rollback_when_vlan_did_not_exist(self, monkeypatch):
        """If VLAN did not exist before delete, rollback must NOT run."""
        created = {"n": 0}
        monkeypatch.setattr(vlan_service, "create_vlan_on_device", lambda *a: created.__setitem__("n", created["n"] + 1) or {"rc": 0})
        performed, success = svc._rollback_delete(10, "dev", "job1", self._pre(False))
        assert performed is False
        assert success is None
        assert created["n"] == 0

    def test_rollback_success_when_vlan_present_after_recreate(self, monkeypatch):
        """Rollback succeeds if VLAN is present after recreate."""
        monkeypatch.setattr(vlan_service, "create_vlan_on_device", lambda *a: {"rc": 0})
        monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: [VLANInfo(vlan_id=10, name="MGMT")])
        performed, success = svc._rollback_delete(10, "dev", "job1", self._pre(True))
        assert performed is True
        assert success is True

    def test_rollback_fails_when_vlan_absent_after_recreate(self, monkeypatch):
        """Verification: if VLAN is still absent after recreate, rollback_success=False."""
        monkeypatch.setattr(vlan_service, "create_vlan_on_device", lambda *a: {"rc": 0})
        monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: [])  # VLAN missing → bad
        performed, success = svc._rollback_delete(10, "dev", "job1", self._pre(True))
        assert performed is True
        assert success is False


class TestRollbackUpdate:
    def _pre(self, has_name):
        if not has_name:
            return {"existed": True, "vlan_data": None}
        return {"existed": True, "vlan_data": {"vlan_id": 10, "name": "MGMT"}}

    def test_no_rollback_when_prev_name_unknown(self, monkeypatch):
        """If pre-state has no previous name, rollback must NOT run."""
        updated = {"n": 0}
        monkeypatch.setattr(vlan_service, "update_vlan_description", lambda *a: updated.__setitem__("n", updated["n"] + 1) or {"rc": 0})
        performed, success = svc._rollback_update(10, "dev", "job1", self._pre(False))
        assert performed is False
        assert success is None
        assert updated["n"] == 0

    def test_rollback_success_when_name_restored(self, monkeypatch):
        """Rollback succeeds if VLAN has the original name after restore."""
        monkeypatch.setattr(vlan_service, "update_vlan_description", lambda *a: {"rc": 0})
        monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: [VLANInfo(vlan_id=10, name="MGMT")])
        performed, success = svc._rollback_update(10, "dev", "job1", self._pre(True))
        assert performed is True
        assert success is True

    def test_rollback_fails_when_name_mismatch_after_restore(self, monkeypatch):
        """Verification: if VLAN name doesn't match original, rollback_success=False."""
        monkeypatch.setattr(vlan_service, "update_vlan_description", lambda *a: {"rc": 0})
        # Name is wrong after restore
        monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: [VLANInfo(vlan_id=10, name="WRONG")])
        performed, success = svc._rollback_update(10, "dev", "job1", self._pre(True))
        assert performed is True
        assert success is False

    def test_rollback_case_insensitive_name_match(self, monkeypatch):
        """Name comparison in verification must be case-insensitive."""
        monkeypatch.setattr(vlan_service, "update_vlan_description", lambda *a: {"rc": 0})
        monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: [VLANInfo(vlan_id=10, name="mgmt")])  # lowercase
        performed, success = svc._rollback_update(10, "dev", "job1", self._pre(True))  # pre_state has "MGMT"
        assert performed is True
        assert success is True


# ── Log message tests ─────────────────────────────────────────────────────────

def test_rollback_started_log_emitted(monkeypatch, caplog):
    """'Rollback started for VLAN X' must be logged on any rollback attempt."""
    monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 0})
    monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: [])
    pre = {"existed": False, "vlan_data": None}
    with caplog.at_level(logging.INFO, logger="app.services.vlan_execution_service"):
        svc._rollback_create(901, "dev", "log-job", pre)
    assert any("rollback started for VLAN 901" in r.message for r in caplog.records)


def test_rollback_verification_succeeded_log(monkeypatch, caplog):
    monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 0})
    monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: [])
    pre = {"existed": False, "vlan_data": None}
    with caplog.at_level(logging.INFO, logger="app.services.vlan_execution_service"):
        svc._rollback_create(901, "dev", "log-job", pre)
    assert any("rollback verification succeeded" in r.message for r in caplog.records)


def test_rollback_verification_failed_log(monkeypatch, caplog):
    monkeypatch.setattr(vlan_service, "delete_vlan", lambda *a: {"rc": 0})
    monkeypatch.setattr(vlan_service, "get_vlans", lambda *a: [VLANInfo(vlan_id=901, name="X")])
    pre = {"existed": False, "vlan_data": None}
    with caplog.at_level(logging.WARNING, logger="app.services.vlan_execution_service"):
        svc._rollback_create(901, "dev", "log-job", pre)
    assert any("rollback verification failed" in r.message for r in caplog.records)


# ── API visibility: rollback_success in job response ─────────────────────────

def test_rollback_success_true_in_job_response(client, monkeypatch):
    """rollback_success=True must appear in the job API response after successful rollback."""
    def _fail_create(vlan_id, name, device_id):
        return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}

    def _succeed_delete(vlan_id, device_id):
        return {"rc": 0, "stdout": "deleted", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", _fail_create)
    monkeypatch.setattr(vlan_service, "delete_vlan", _succeed_delete)
    # get_vlans: VLAN 901 absent → verification succeeds (it's not in mock list by default)

    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 901, "name": "ROLLBACK_SUCCESS", "devices": ["mock_device"]},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["job_id"]
    time.sleep(0.3)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["rollback_performed"] is True
    assert job["rollback_success"] is True


def test_rollback_success_false_in_job_response(client, monkeypatch):
    """rollback_success=False must appear when rollback ran but verification failed."""
    def _fail_create(vlan_id, name, device_id):
        return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}

    def _succeed_delete(vlan_id, device_id):
        return {"rc": 0, "stdout": "deleted", "stderr": ""}

    # Bypass pre-state capture so monkeypatching get_vlans only affects rollback verification.
    # Without this, get_vlans (called during enqueue pre-state) would return VLAN 901 as
    # already existing, sending the job down the validation-failure path before Ansible runs.
    import app.api.vlans as vlans_api
    monkeypatch.setattr(
        vlans_api, "_capture_pre_state_vlan",
        lambda vlan_id, device: {"existed": False, "vlan_data": None},
    )

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", _fail_create)
    monkeypatch.setattr(vlan_service, "delete_vlan", _succeed_delete)
    # After delete rc=0, get_vlans still shows VLAN present → verification fails
    monkeypatch.setattr(
        vlan_service, "get_vlans",
        lambda *a: [VLANInfo(vlan_id=901, name="ROLLBACK_TEST")],
    )

    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 901, "name": "ROLLBACK_FAIL", "devices": ["mock_device"]},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["job_id"]
    time.sleep(0.3)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "failed"
    assert job["rollback_performed"] is True   # rollback delete was executed
    assert job["rollback_success"] is False    # but verification found VLAN still present


def test_rollback_success_null_when_no_rollback(client):
    """rollback_success must be null when the operation succeeds (no rollback needed)."""
    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 200, "name": "SUCCESS_VLAN", "devices": ["mock_device"]},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobs"][0]["job_id"]
    time.sleep(0.3)

    job = client.get(f"/api/v1/jobs/{job_id}").json()["data"]
    assert job["status"] == "completed"
    assert job["rollback_performed"] is False
    assert job["rollback_success"] is None


def test_rollback_success_in_audit_event(client, monkeypatch):
    """rollback_success must be recorded in the audit event details."""
    def _fail_create(vlan_id, name, device_id):
        return {"rc": 1, "stdout": "", "stderr": "configuration syntax error"}

    def _succeed_delete(vlan_id, device_id):
        return {"rc": 0, "stdout": "deleted", "stderr": ""}

    monkeypatch.setattr(vlan_service, "create_vlan_on_device", _fail_create)
    monkeypatch.setattr(vlan_service, "delete_vlan", _succeed_delete)
    # VLAN 901 absent from mock list → verification passes

    resp = client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 901, "name": "AUDIT_CHECK", "devices": ["mock_device"]},
    )
    job_id = resp.json()["jobs"][0]["job_id"]
    time.sleep(0.3)

    log = audit_service.get_audit_log()
    entry = next(e for e in log if e.job_id == job_id)
    assert entry.details.get("rollback_performed") is True
    assert entry.details.get("rollback_success") is True
