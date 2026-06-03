"""Step 7.4 — Site-aware device groups."""
import pytest

from app.db.models import (
    DeviceGroupMemberModel,
    DeviceGroupModel,
    DeviceModel,
    SiteModel,
    UserAllowedSiteModel,
    UserModel,
)
from app.db.session import get_session
from app.schemas.user import UserCreate
from app.services import user_service


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_site(admin_client, name: str) -> int:
    r = admin_client.post("/api/v1/sites/", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["data"]["id"]


def _attach(device_name: str, site_id: int | None) -> None:
    with get_session() as session:
        dev = session.query(DeviceModel).filter_by(name=device_name).first()
        assert dev is not None
        dev.site_id = site_id


def _grant(username: str, site_ids: list[int]) -> None:
    with get_session() as session:
        row = session.query(UserModel).filter_by(username=username).first()
        assert row is not None
        session.query(UserAllowedSiteModel).filter_by(user_id=row.id).delete(synchronize_session=False)
        for sid in site_ids:
            session.add(UserAllowedSiteModel(user_id=row.id, site_id=sid))


@pytest.fixture(autouse=True)
def _clean():
    with get_session() as session:
        session.query(DeviceGroupMemberModel).delete(synchronize_session=False)
        session.query(DeviceGroupModel).delete(synchronize_session=False)
        session.query(DeviceModel).update({DeviceModel.site_id: None}, synchronize_session=False)
        session.query(UserAllowedSiteModel).delete(synchronize_session=False)
        session.query(SiteModel).delete(synchronize_session=False)
    yield


# ── Group creation requires site_id ──────────────────────────────────────────

def test_create_group_requires_site_id(admin_client):
    r = admin_client.post("/api/v1/device-groups/", json={"name": "no-site"})
    # site_id is required → pydantic 422 (or 400 from validation handler if it converts)
    assert r.status_code in (400, 422), r.text


def test_create_group_with_unknown_site_rejected(admin_client):
    r = admin_client.post("/api/v1/device-groups/", json={"name": "g", "site_id": 99999})
    assert r.status_code == 400, r.text


def test_create_valid_group_returns_site_metadata(admin_client):
    lib = _seed_site(admin_client, "Library")
    r = admin_client.post("/api/v1/device-groups/", json={"name": "LibraryGroup", "site_id": lib})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["site_id"] == lib
    assert data["site_name"] == "Library"


# ── Membership invariant: device.site must match group.site ──────────────────

def test_add_member_same_site_succeeds(admin_client):
    lib = _seed_site(admin_client, "Library")
    _attach("mock_device", lib)
    gid = admin_client.post("/api/v1/device-groups/", json={"name": "G", "site_id": lib}).json()["data"]["id"]
    r = admin_client.post(f"/api/v1/device-groups/{gid}/members", json={"device_name": "mock_device"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["added"] is True


def test_add_member_different_site_rejected_400(admin_client):
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    _attach("fail_device", lab)
    gid = admin_client.post("/api/v1/device-groups/", json={"name": "LibGroup", "site_id": lib}).json()["data"]["id"]
    r = admin_client.post(f"/api/v1/device-groups/{gid}/members", json={"device_name": "fail_device"})
    assert r.status_code == 400, r.text
    assert "site" in r.json()["message"].lower()


def test_add_unassigned_device_to_group_rejected_400(admin_client):
    lib = _seed_site(admin_client, "Library")
    # mock_device intentionally left unassigned
    gid = admin_client.post("/api/v1/device-groups/", json={"name": "G", "site_id": lib}).json()["data"]["id"]
    r = admin_client.post(f"/api/v1/device-groups/{gid}/members", json={"device_name": "mock_device"})
    assert r.status_code == 400


# ── Reassigning a device's site must respect group memberships ───────────────

def test_device_site_change_blocked_when_in_group(admin_client):
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    gid = admin_client.post("/api/v1/device-groups/", json={"name": "G", "site_id": lib}).json()["data"]["id"]
    admin_client.post(f"/api/v1/device-groups/{gid}/members", json={"device_name": "mock_device"})

    # Attempting to move the device to a different site must be rejected.
    r = admin_client.put("/api/v1/devices/mock_device", json={"site_id": lab})
    assert r.status_code == 400, r.text
    assert "group" in r.json()["message"].lower()


def test_device_site_change_clear_blocked_when_in_group(admin_client):
    lib = _seed_site(admin_client, "Library")
    _attach("mock_device", lib)
    gid = admin_client.post("/api/v1/device-groups/", json={"name": "G", "site_id": lib}).json()["data"]["id"]
    admin_client.post(f"/api/v1/device-groups/{gid}/members", json={"device_name": "mock_device"})

    # Setting site_id=null when the group requires a site → reject.
    r = admin_client.put("/api/v1/devices/mock_device", json={"site_id": None})
    assert r.status_code == 400


def test_device_site_change_allowed_after_removing_from_group(admin_client):
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    gid = admin_client.post("/api/v1/device-groups/", json={"name": "G", "site_id": lib}).json()["data"]["id"]
    admin_client.post(f"/api/v1/device-groups/{gid}/members", json={"device_name": "mock_device"})

    # Remove from the group, then the device can be reassigned freely.
    admin_client.delete(f"/api/v1/device-groups/{gid}/members/mock_device")
    r = admin_client.put("/api/v1/devices/mock_device", json={"site_id": lab})
    assert r.status_code == 200


# ── RBAC: filter by group.site_id ────────────────────────────────────────────

@pytest.fixture
def juan(admin_client):
    from fastapi.testclient import TestClient
    from app.core.security import create_access_token
    from app.main import app

    if user_service.get_by_username("juan") is None:
        user_service.create_user(UserCreate(username="juan", password="juan_pass_99", role="operator"))
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {create_access_token({'sub':'juan','role':'operator'})}"})
    return c


def test_restricted_user_sees_only_allowed_site_groups(juan, admin_client):
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _attach("mock_device", lib)
    _attach("fail_device", lab)
    _grant("juan", [lib])

    g_lib = admin_client.post("/api/v1/device-groups/", json={"name": "LibG", "site_id": lib}).json()["data"]["id"]
    g_lab = admin_client.post("/api/v1/device-groups/", json={"name": "LabG", "site_id": lab}).json()["data"]["id"]

    r = juan.get("/api/v1/device-groups/")
    ids = {g["id"] for g in r.json()["data"]}
    assert g_lib in ids
    assert g_lab not in ids


def test_restricted_user_403_on_forbidden_group_get(juan, admin_client):
    lib = _seed_site(admin_client, "Library")
    lab = _seed_site(admin_client, "Laboratory")
    _grant("juan", [lib])
    g_lab = admin_client.post("/api/v1/device-groups/", json={"name": "LabG", "site_id": lab}).json()["data"]["id"]

    assert juan.get(f"/api/v1/device-groups/{g_lab}").status_code == 403
    assert juan.get(f"/api/v1/device-groups/{g_lab}/devices").status_code == 403


def test_admin_sees_all_groups_including_legacy(admin_client):
    # Legacy NULL-site group exists directly via the model.
    lib = _seed_site(admin_client, "Library")
    with get_session() as session:
        legacy = DeviceGroupModel(name="Legacy", description=None, site_id=None)
        session.add(legacy)
        session.flush()
        legacy_id = legacy.id

    g_lib = admin_client.post("/api/v1/device-groups/", json={"name": "LibG", "site_id": lib}).json()["data"]["id"]

    ids = {g["id"] for g in admin_client.get("/api/v1/device-groups/").json()["data"]}
    assert legacy_id in ids
    assert g_lib in ids


# ── Backfill migration on legacy single-site groups ──────────────────────────

# SQL copied verbatim from migration a3c7e9d1f482 (device_group_site_id). Kept
# inline so the tests below verify the backfill semantics against the same
# statement the migration would execute, without having to re-run Alembic.
_BACKFILL_SQL = """
    UPDATE device_groups
       SET site_id = (
           SELECT MIN(d.site_id)
             FROM device_group_members m
             JOIN devices d ON d.name = m.device_name
            WHERE m.group_id = device_groups.id
              AND d.site_id IS NOT NULL
       )
     WHERE site_id IS NULL
       AND id IN (
           SELECT m.group_id
             FROM device_group_members m
             JOIN devices d ON d.name = m.device_name
            GROUP BY m.group_id
           HAVING MIN(d.site_id) = MAX(d.site_id)
              AND MIN(d.site_id) IS NOT NULL
       )
"""


def _run_device_group_site_backfill() -> None:
    from sqlalchemy import text
    with get_session() as session:
        session.execute(text(_BACKFILL_SQL))


def test_backfill_legacy_group_gets_site_id():
    """Verifies the backfill SQL from migration a3c7e9d1f482 picks up
    single-site legacy groups."""
    with get_session() as session:
        site = SiteModel(name="MigrateSite")
        session.add(site)
        session.flush()
        sid = site.id
        dev = session.query(DeviceModel).filter_by(name="mock_device").first()
        dev.site_id = sid
        g = DeviceGroupModel(name="LegacyOK", description=None, site_id=None)
        session.add(g)
        session.flush()
        session.add(DeviceGroupMemberModel(group_id=g.id, device_name="mock_device"))
        gid = g.id

    _run_device_group_site_backfill()

    with get_session() as session:
        g = session.query(DeviceGroupModel).filter_by(id=gid).first()
        assert g.site_id == sid


def test_backfill_legacy_mixed_group_stays_null():
    with get_session() as session:
        site_a = SiteModel(name="MA")
        site_b = SiteModel(name="MB")
        session.add(site_a); session.add(site_b); session.flush()
        a_id, b_id = site_a.id, site_b.id
        session.query(DeviceModel).filter_by(name="mock_device").update({DeviceModel.site_id: a_id})
        session.query(DeviceModel).filter_by(name="fail_device").update({DeviceModel.site_id: b_id})
        g = DeviceGroupModel(name="LegacyMixed", description=None, site_id=None)
        session.add(g); session.flush()
        session.add(DeviceGroupMemberModel(group_id=g.id, device_name="mock_device"))
        session.add(DeviceGroupMemberModel(group_id=g.id, device_name="fail_device"))
        gid = g.id

    _run_device_group_site_backfill()

    with get_session() as session:
        g = session.query(DeviceGroupModel).filter_by(id=gid).first()
        assert g.site_id is None, "mixed-site legacy groups must NOT be backfilled"
