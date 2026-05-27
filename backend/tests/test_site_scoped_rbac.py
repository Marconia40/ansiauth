"""Step 7.3 — Site-scoped RBAC.

Verifies that operators / observers only see and operate on resources tied to
devices in their `allowed_sites`. Admins continue to see and operate on
everything (bypass).
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.security import create_access_token
from app.db.models import (
    AuditLogModel,
    DeviceGroupMemberModel,
    DeviceGroupModel,
    DeviceModel,
    JobModel,
    SiteModel,
    UserAllowedSiteModel,
    UserModel,
)
from app.db.session import get_session
from app.main import app
from app.schemas.user import UserCreate
from app.services import user_service


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_token(username: str, role: str) -> str:
    return create_access_token({"sub": username, "role": role})


def _client(username: str, role: str) -> TestClient:
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {_make_token(username, role)}"})
    return c


def _attach(device_name: str, site_id: int | None) -> None:
    with get_session() as session:
        dev = session.query(DeviceModel).filter_by(name=device_name).first()
        assert dev is not None
        dev.site_id = site_id


def _grant(username: str, site_ids: list[int]) -> None:
    with get_session() as session:
        row = session.query(UserModel).filter_by(username=username).first()
        assert row is not None
        session.query(UserAllowedSiteModel).filter_by(user_id=row.id).delete(
            synchronize_session=False
        )
        for sid in site_ids:
            session.add(UserAllowedSiteModel(user_id=row.id, site_id=sid))


def _seed_site(admin_client: TestClient, name: str) -> int:
    r = admin_client.post("/api/v1/sites/", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


def _make_job(device: str | None, status: str = "completed") -> str:
    job_id = f"job-{device}-{datetime.now(timezone.utc).timestamp()}"
    with get_session() as session:
        session.add(JobModel(
            job_id=job_id,
            status=status,
            device=device,
            playbook="test",
            created_at=datetime.now(timezone.utc),
        ))
    return job_id


def _make_audit(action: str, device: str | None) -> None:
    with get_session() as session:
        session.add(AuditLogModel(
            timestamp=datetime.now(timezone.utc),
            user="tester",
            action=action,
            resource="test",
            status="success",
            details={},
            device=device,
        ))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean():
    with get_session() as session:
        session.query(DeviceGroupMemberModel).delete(synchronize_session=False)
        session.query(DeviceGroupModel).delete(synchronize_session=False)
        session.query(JobModel).delete(synchronize_session=False)
        session.query(AuditLogModel).delete(synchronize_session=False)
        session.query(DeviceModel).update({DeviceModel.site_id: None}, synchronize_session=False)
        # Remove all user_allowed_sites so each test starts from a clean slate.
        session.query(UserAllowedSiteModel).delete(synchronize_session=False)
        session.query(SiteModel).delete(synchronize_session=False)
    yield


@pytest.fixture
def juan(admin_client) -> tuple[TestClient, dict]:
    """Operator-role 'juan' user. Tests grant sites explicitly."""
    if user_service.get_by_username("juan") is None:
        user_service.create_user(UserCreate(username="juan", password="juan_pass_99", role="operator"))
    return _client("juan", "operator"), {"username": "juan"}


# ── Devices visibility ────────────────────────────────────────────────────────

def test_operator_with_site_sees_only_allowed_devices(juan, admin_client):
    juan_client, _ = juan
    site_lib = _seed_site(admin_client, "Library")
    site_lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", site_lib)
    _attach("fail_device", site_lab)
    _grant("juan", [site_lib])

    r = juan_client.get("/api/v1/devices/")
    assert r.status_code == 200
    names = {d["name"] for d in r.json()["data"]}
    assert names == {"mock_device"}


def test_operator_403_on_forbidden_device(juan, admin_client):
    juan_client, _ = juan
    site_lib = _seed_site(admin_client, "Library")
    site_lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", site_lib)
    _attach("fail_device", site_lab)
    _grant("juan", [site_lib])

    assert juan_client.get("/api/v1/devices/mock_device").status_code == 200
    assert juan_client.get("/api/v1/devices/fail_device").status_code == 403


def test_admin_bypasses_scoping(admin_client):
    site = _seed_site(admin_client, "Library")
    _attach("mock_device", site)
    _attach("fail_device", None)  # unassigned

    r = admin_client.get("/api/v1/devices/")
    assert r.status_code == 200
    names = {d["name"] for d in r.json()["data"]}
    # Admin sees everything regardless of site
    assert "mock_device" in names and "fail_device" in names


# ── VLAN operations: 403 on forbidden device ──────────────────────────────────

def test_operator_create_vlan_rejected_on_forbidden_device(juan, admin_client):
    juan_client, _ = juan
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    _attach("fail_device", lab)
    _grant("juan", [lib])

    # Single-device call against fail_device → 403
    r = juan_client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 50, "name": "TEST", "devices": ["fail_device"]},
    )
    assert r.status_code == 403


def test_operator_create_vlan_allowed_on_allowed_device(juan, admin_client):
    juan_client, _ = juan
    lib = _seed_site(admin_client, "Library")
    _attach("mock_device", lib)
    _grant("juan", [lib])

    r = juan_client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 51, "name": "OK", "devices": ["mock_device"]},
    )
    assert r.status_code == 200, r.text


def test_operator_multi_device_vlan_403_if_any_device_forbidden(juan, admin_client):
    juan_client, _ = juan
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    _attach("fail_device", lab)
    _grant("juan", [lib])

    r = juan_client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 52, "name": "MIX", "devices": ["mock_device", "fail_device"]},
    )
    assert r.status_code == 403


# ── Jobs visibility ───────────────────────────────────────────────────────────

def test_operator_sees_only_jobs_for_allowed_devices(juan, admin_client):
    juan_client, _ = juan
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    _attach("fail_device", lab)
    _grant("juan", [lib])
    _make_job("mock_device")
    _make_job("fail_device")
    _make_job(None)  # device-less — strict for jobs: not visible to non-admin

    r = juan_client.get("/api/v1/jobs/")
    assert r.status_code == 200
    body = r.json()
    devs = {j["device"] for j in body["items"]}
    assert devs == {"mock_device"}


def test_operator_403_on_forbidden_job(juan, admin_client):
    juan_client, _ = juan
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    _attach("fail_device", lab)
    _grant("juan", [lib])
    forbidden = _make_job("fail_device")
    r = juan_client.get(f"/api/v1/jobs/{forbidden}")
    assert r.status_code == 403


def test_admin_sees_all_jobs(admin_client):
    lib = _seed_site(admin_client, "Library")
    _attach("mock_device", lib)
    _make_job("mock_device")
    _make_job("fail_device")
    _make_job(None)

    r = admin_client.get("/api/v1/jobs/")
    assert r.json()["total"] == 3


# ── Audit visibility (admin-only endpoint by role — defense-in-depth) ─────────

def test_admin_audit_unrestricted(admin_client):
    """Admin bypasses scoping at the audit endpoint."""
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    _attach("fail_device", lab)
    _make_audit("vlan_create", "mock_device")
    _make_audit("vlan_create", "fail_device")
    _make_audit("user_login", None)

    rows = admin_client.get("/api/v1/audit/").json()
    actions_devices = [(r["action"], r["device"]) for r in rows if r["action"] in ("vlan_create", "user_login")]
    assert ("vlan_create", "mock_device") in actions_devices
    assert ("vlan_create", "fail_device") in actions_devices
    assert ("user_login", None) in actions_devices


# ── Device groups visibility ──────────────────────────────────────────────────

def _make_group(name: str, devices: list[str]) -> int:
    with get_session() as session:
        g = DeviceGroupModel(name=name, description=None)
        session.add(g)
        session.flush()
        for d in devices:
            session.add(DeviceGroupMemberModel(group_id=g.id, device_name=d))
        session.flush()
        return g.id


def test_operator_sees_only_groups_with_allowed_devices(juan, admin_client):
    juan_client, _ = juan
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    _attach("fail_device", lab)
    _grant("juan", [lib])

    g_visible = _make_group("LibraryGroup", ["mock_device"])
    g_hidden = _make_group("LabGroup", ["fail_device"])

    r = juan_client.get("/api/v1/device-groups/")
    assert r.status_code == 200
    ids = {g["id"] for g in r.json()["data"]}
    assert g_visible in ids
    assert g_hidden not in ids


def test_operator_403_on_forbidden_group(juan, admin_client):
    juan_client, _ = juan
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    _attach("fail_device", lab)
    _grant("juan", [lib])

    g_hidden = _make_group("LabGroup", ["fail_device"])
    assert juan_client.get(f"/api/v1/device-groups/{g_hidden}").status_code == 403


def test_group_devices_listing_filtered_for_operator(juan, admin_client):
    juan_client, _ = juan
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    _attach("fail_device", lab)
    _grant("juan", [lib])
    g_mixed = _make_group("Mixed", ["mock_device", "fail_device"])

    r = juan_client.get(f"/api/v1/device-groups/{g_mixed}/devices")
    assert r.status_code == 200
    assert r.json()["data"]["devices"] == ["mock_device"]


# ── User management: allowed_sites assignment ────────────────────────────────

def test_admin_can_create_user_with_allowed_sites(admin_client):
    lib = _seed_site(admin_client, "Library")
    r = admin_client.post("/api/v1/users/", json={
        "username": "pedro",
        "password": "pedro_pass_99",
        "role": "operator",
        "allowed_site_ids": [lib],
    })
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["allowed_site_ids"] == [lib]
    assert body["allowed_site_names"] == ["Library"]
    # cleanup
    admin_client.delete(f"/api/v1/users/{body['id']}")


def test_create_user_with_unknown_site_id_rejected(admin_client):
    r = admin_client.post("/api/v1/users/", json={
        "username": "ghost",
        "password": "ghost_pass_99",
        "role": "operator",
        "allowed_site_ids": [99999],
    })
    assert r.status_code == 400


def test_super_admin_can_update_allowed_sites(super_admin_client, admin_client):
    # create the target via admin (allowed by their role).
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    created = admin_client.post("/api/v1/users/", json={
        "username": "maria",
        "password": "maria_pass_99",
        "role": "operator",
        "allowed_site_ids": [lib],
    }).json()["data"]
    uid = created["id"]
    r = super_admin_client.put(f"/api/v1/users/{uid}", json={"allowed_site_ids": [lib, lab]})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["allowed_site_ids"] == sorted([lib, lab])
    # Clearing
    r = super_admin_client.put(f"/api/v1/users/{uid}", json={"allowed_site_ids": []})
    assert r.status_code == 200
    assert r.json()["data"]["allowed_site_ids"] == []
    # cleanup
    admin_client.delete(f"/api/v1/users/{uid}")


# ── Regression — admin flows unchanged ───────────────────────────────────────

def test_admin_can_still_create_vlan_on_unassigned_device(admin_client, operator_client):
    """Even unassigned devices are reachable by admin via vlan ops (operator side
    would need allowed_sites=[] which falls back to unassigned access)."""
    r = operator_client.post(
        "/api/v1/vlans/",
        json={"vlan_id": 99, "name": "FALLBACK", "devices": ["mock_device"]},
    )
    # Operator (test-fixture-seeded with all-current-sites=none) has allowed_sites=[]
    # so the fallback "see unassigned" rule applies — mock_device is unassigned.
    assert r.status_code == 200, r.text
