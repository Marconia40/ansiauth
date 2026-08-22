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
    """MSP: Phase 4 — moving a device now means moving both site_id AND
    device_group_id. When ``site_id`` is not None the device lands in that
    site's Default group; None is only accepted when the device is being
    unhooked (M3 NOT NULL is bypassed via a special sentinel that assumes
    the caller will re-attach before commit)."""
    with get_session() as session:
        dev = session.query(DeviceModel).filter_by(name=device_name).first()
        assert dev is not None
        if site_id is None:
            # Move device to Base-Infra's Default group so device_group_id
            # stays valid under M3's NOT NULL constraint.
            from app.services.site_service import BASE_INFRA_SITE_KIND
            base = session.query(
                SiteModel.id, SiteModel.default_group_id
            ).filter(SiteModel.kind == BASE_INFRA_SITE_KIND).first()
            assert base is not None
            dev.site_id = base[0]
            dev.device_group_id = base[1]
        else:
            row = session.query(SiteModel).filter_by(id=site_id).first()
            assert row is not None, f"site {site_id} does not exist"
            assert row.default_group_id is not None, (
                f"site {site_id} has no default group — create via the API "
                "so the site+group are provisioned atomically"
            )
            dev.site_id = site_id
            dev.device_group_id = row.default_group_id


def _grant(username: str, site_ids: list[int]) -> None:
    """MSP: Phase 4 — write both legacy ``UserAllowedSiteModel`` (kept for
    T3.5 snapshot-diff parity) and ``RoleAssignmentModel`` (authoritative
    post-Phase 3) so authz decisions succeed under both flag states."""
    from app.db.models import RoleAssignmentModel
    with get_session() as session:
        row = session.query(UserModel).filter_by(username=username).first()
        assert row is not None
        session.query(UserAllowedSiteModel).filter_by(user_id=row.id).delete(
            synchronize_session=False
        )
        session.query(RoleAssignmentModel).filter(
            RoleAssignmentModel.user_id == row.id,
            RoleAssignmentModel.device_group_id.is_(None),
        ).delete(synchronize_session=False)
        for sid in site_ids:
            session.add(UserAllowedSiteModel(user_id=row.id, site_id=sid))
            session.add(RoleAssignmentModel(
                user_id=row.id, site_id=sid,
                device_group_id=None, role=row.role,
            ))


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
    """MSP: Phase 4 — SQLite now enforces FKs. Order matters:

    1. Delete devices (before their group's FK RESTRICT fires).
    2. Delete member rows + role_assignments.
    3. Null sites.default_group_id before deleting groups so the RESTRICT
       from sites.default_group_id → device_groups doesn't fire.
    4. Delete groups, then sites.
    Base-Infrastructure + mock devices are then re-seeded so the rest of
    the fixture set (conftest's role-user seeding, seed_defaults) has the
    baseline it expects.
    """
    from app.db.models import RoleAssignmentModel
    from app.services import device_service, site_service
    with get_session() as session:
        session.query(DeviceGroupMemberModel).delete(synchronize_session=False)
        session.query(DeviceModel).delete(synchronize_session=False)
        session.query(JobModel).delete(synchronize_session=False)
        session.query(AuditLogModel).delete(synchronize_session=False)
        session.query(RoleAssignmentModel).delete(synchronize_session=False)
        session.query(UserAllowedSiteModel).delete(synchronize_session=False)
        session.query(SiteModel).update(
            {SiteModel.default_group_id: None}, synchronize_session=False,
        )
        session.query(DeviceGroupModel).delete(synchronize_session=False)
        session.query(SiteModel).delete(synchronize_session=False)
    site_service.ensure_base_infrastructure()
    device_service.seed_defaults()
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


def test_operator_denied_on_forbidden_device(juan, admin_client):
    """MSP: Phase 4 — a forbidden device returns 404 (not 403) under
    MSP-strict so callers can't probe existence. Accept both for the
    duration of the flag-off compatibility window."""
    juan_client, _ = juan
    site_lib = _seed_site(admin_client, "Library")
    site_lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", site_lib)
    _attach("fail_device", site_lab)
    _grant("juan", [site_lib])

    assert juan_client.get("/api/v1/devices/mock_device").status_code == 200
    assert juan_client.get("/api/v1/devices/fail_device").status_code in (403, 404)


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

def _make_group(name: str, devices: list[str], site_id: int | None = None) -> int:
    """Create a device group. Step 7.4: groups must belong to a site; passing
    site_id=None creates a legacy / mixed-site group (admin-only by policy)."""
    with get_session() as session:
        g = DeviceGroupModel(name=name, description=None, site_id=site_id)
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

    g_visible = _make_group("LibraryGroup", ["mock_device"], site_id=lib)
    g_hidden = _make_group("LabGroup", ["fail_device"], site_id=lab)

    r = juan_client.get("/api/v1/device-groups/")
    assert r.status_code == 200
    ids = {g["id"] for g in r.json()["data"]}
    assert g_visible in ids
    assert g_hidden not in ids


def test_operator_denied_on_forbidden_group(juan, admin_client):
    """MSP: Phase 4 — group GET returns 404 (not 403) when the caller has
    no scope, hiding existence. Accept both for the flag-off compat window."""
    juan_client, _ = juan
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    _attach("fail_device", lab)
    _grant("juan", [lib])

    g_hidden = _make_group("LabGroup", ["fail_device"], site_id=lab)
    assert juan_client.get(f"/api/v1/device-groups/{g_hidden}").status_code in (403, 404)


def test_group_devices_listing_includes_all_same_site_devices(juan, admin_client):
    """A site-aware group's members all share that site by construction, so an
    operator allowed for that site sees the full membership."""
    juan_client, _ = juan
    lib = _seed_site(admin_client, "Library")
    _attach("mock_device", lib)
    _attach("fail_device", lib)
    _grant("juan", [lib])
    gid = _make_group("LibraryGroup", ["mock_device", "fail_device"], site_id=lib)

    r = juan_client.get(f"/api/v1/device-groups/{gid}/devices")
    assert r.status_code == 200
    assert sorted(r.json()["data"]["devices"]) == ["fail_device", "mock_device"]


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

@pytest.mark.skip(
    reason=(
        "MSP: Phase 4 — the legacy 'operator with empty allowed_sites falls "
        "back to unassigned devices' rule no longer exists. Devices always "
        "have a NOT NULL device_group_id post-M3; scope decisions come from "
        "role_assignments only. Superseded by test_msp_effective_role.py."
    ),
)
def test_admin_can_still_create_vlan_on_unassigned_device(admin_client, operator_client):
    pass
