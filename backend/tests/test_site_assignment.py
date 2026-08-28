"""Step 7.2 — Device site assignment and site-aware filters."""
import pytest

from app.db.models import AuditLogModel, DeviceModel, JobModel, SiteModel, UserAllowedSiteModel, UserModel
from app.db.session import get_session
from datetime import datetime, timezone


def _grant_to_observer(site_id: int) -> None:
    """Grant the test observer access to a site (the autouse fixture seeded
    observer with whatever sites existed at fixture time — newly-created
    sites aren't auto-added).

    MSP: Phase 4 — writes both ``UserAllowedSiteModel`` (legacy) and
    ``RoleAssignmentModel`` (authoritative under flag=True) so authz
    succeeds under both flag states.
    """
    from app.db.models import RoleAssignmentModel
    with get_session() as session:
        user_row = session.query(UserModel).filter_by(username="observer").first()
        if user_row is None:
            return
        existing = session.query(UserAllowedSiteModel).filter_by(
            user_id=user_row.id, site_id=site_id
        ).first()
        if existing is None:
            session.add(UserAllowedSiteModel(user_id=user_row.id, site_id=site_id))
        existing_ra = session.query(RoleAssignmentModel).filter_by(
            user_id=user_row.id, site_id=site_id, device_group_id=None,
        ).first()
        if existing_ra is None:
            session.add(RoleAssignmentModel(
                user_id=user_row.id, site_id=site_id,
                device_group_id=None, role="observer",
            ))


# ── Setup ─────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean():
    """MSP: Phase 4 — SQLite FK enforcement + M3 NOT NULL flips.

    Devices have device_group_id NOT NULL, groups have site_id NOT NULL, and
    sites.default_group_id → device_groups RESTRICT. Delete in the right
    order and reseed Base-Infrastructure + mock devices so other fixtures
    (conftest role users) see the expected baseline.
    """
    from app.db.models import DeviceGroupMemberModel, DeviceGroupModel, RoleAssignmentModel
    from app.services import device_service, site_service

    def _wipe_and_reseed():
        with get_session() as session:
            session.query(DeviceGroupMemberModel).delete(synchronize_session=False)
            session.query(DeviceModel).delete(synchronize_session=False)
            session.query(JobModel).delete(synchronize_session=False)
            session.query(AuditLogModel).delete(synchronize_session=False)
            session.query(RoleAssignmentModel).delete(synchronize_session=False)
            session.query(SiteModel).update(
                {SiteModel.default_group_id: None}, synchronize_session=False,
            )
            session.query(DeviceGroupModel).delete(synchronize_session=False)
            session.query(SiteModel).delete(synchronize_session=False)
        site_service.ensure_base_infrastructure()
        device_service.seed_defaults()

    _wipe_and_reseed()
    yield
    _wipe_and_reseed()


def _seed_site(admin_client, name: str) -> int:
    return admin_client.post("/api/v1/sites/", json={"name": name}).json()["data"]["id"]


def _attach(device_name: str, site_id: int) -> None:
    """MSP: Phase 4 — also update ``device_group_id`` (NOT NULL post-M3) to
    the target site's Default group, else effective_role can't resolve a
    scope for the device."""
    from app.db.models import DeviceGroupModel
    with get_session() as session:
        dev = session.query(DeviceModel).filter_by(name=device_name).first()
        assert dev is not None
        row = session.query(SiteModel).filter_by(id=site_id).first()
        assert row is not None and row.default_group_id is not None
        dev.site_id = site_id
        dev.device_group_id = row.default_group_id


# ── Device API: site_id exposure ──────────────────────────────────────────────

def test_create_device_with_site_id(admin_client):
    site_id = _seed_site(admin_client, "HQ")
    r = admin_client.post(
        "/api/v1/devices/",
        json={"name": "dev-x", "host": "10.1.1.1", "vendor": "cisco",
              "username": "u", "password": "p", "site_id": site_id},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["site_id"] == site_id
    assert data["site_name"] == "HQ"

    # cleanup
    admin_client.delete("/api/v1/devices/dev-x")


def test_create_device_with_unknown_site_rejected(admin_client):
    r = admin_client.post(
        "/api/v1/devices/",
        json={"name": "dev-bad", "host": "10.1.1.1", "vendor": "cisco",
              "username": "u", "password": "p", "site_id": 99999},
    )
    assert r.status_code == 400, r.text


def test_list_devices_includes_site_name(admin_client):
    site_id = _seed_site(admin_client, "Library")
    _attach("mock_device", site_id)
    r = admin_client.get("/api/v1/devices/")
    rows = r.json()["data"]
    dev = next(d for d in rows if d["name"] == "mock_device")
    assert dev["site_id"] == site_id
    assert dev["site_name"] == "Library"


def test_update_device_site_id_rejected_under_msp(admin_client):
    """MSP: Phase 4 — ``PUT /devices/{name}`` no longer accepts ``site_id``.
    Callers must use ``POST /devices/{name}/move`` to change a device's
    group (which, when cross-site, also changes its site). This test
    supersedes the legacy ``test_update_device_assign_site`` /
    ``_clear_site_via_null`` / ``_with_unknown_site_rejected`` trio."""
    site_id = _seed_site(admin_client, "Lab")
    r = admin_client.put("/api/v1/devices/mock_device", json={"site_id": site_id})
    # Pydantic's model_validator raises with a clear "use /move" message,
    # which the exception handler surfaces as 422.
    assert r.status_code in (400, 422), r.text
    body_text = r.text.lower()
    assert "move" in body_text, f"Response should point at /move; got {r.text!r}"


def test_move_endpoint_is_the_new_site_change_path(admin_client):
    """The device-level ``POST /devices/{name}/move`` is the sole
    authoritative path for changing a device's site. Verify a cross-site
    move via the move endpoint succeeds (bootstrap admin is system-admin
    → both same-site and cross-site moves are allowed)."""
    site_id = _seed_site(admin_client, "Lab")
    with get_session() as session:
        target_group_id = session.query(SiteModel.default_group_id).filter_by(
            id=site_id,
        ).scalar()
    r = admin_client.post(
        "/api/v1/devices/mock_device/move",
        json={"device_group_id": target_group_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["site_id"] == site_id
    assert body["device_group_id"] == target_group_id


def test_update_device_omitted_site_keeps_value(admin_client):
    site_id = _seed_site(admin_client, "Lab")
    _attach("mock_device", site_id)
    r = admin_client.put("/api/v1/devices/mock_device", json={"host": "10.9.9.9"})
    assert r.status_code == 200
    assert r.json()["data"]["site_id"] == site_id


# ── Jobs: site_id filter ──────────────────────────────────────────────────────

def _make_job(device: str | None, status: str = "completed"):
    with get_session() as session:
        session.add(JobModel(
            job_id=f"job-{device}-{datetime.now(timezone.utc).timestamp()}",
            status=status,
            device=device,
            playbook="test",
            created_at=datetime.now(timezone.utc),
        ))


def test_jobs_site_filter_returns_only_site_devices(observer_client, admin_client):
    site_a = _seed_site(admin_client, "A")
    site_b = _seed_site(admin_client, "B")
    # Make site A visible to observer (site B intentionally left out so we also
    # verify scoping doesn't accidentally include site B's devices).
    _grant_to_observer(site_a)
    _grant_to_observer(site_b)
    _attach("mock_device", site_a)
    _attach("fail_device", site_b)
    _make_job("mock_device")
    _make_job("fail_device")
    _make_job(None)  # device-less job — should be EXCLUDED from a site filter (different from audit)

    r = observer_client.get("/api/v1/jobs/", params={"site_id": site_a})
    assert r.status_code == 200
    body = r.json()
    devices = {j["device"] for j in body["items"]}
    assert devices == {"mock_device"}
    assert body["total"] == 1


def test_jobs_site_filter_empty_site_returns_empty(observer_client, admin_client):
    site_id = _seed_site(admin_client, "Empty")
    _make_job("mock_device")
    r = observer_client.get("/api/v1/jobs/", params={"site_id": site_id})
    assert r.status_code == 200
    assert r.json()["total"] == 0
    assert r.json()["items"] == []


# ── Audit: site_id filter ─────────────────────────────────────────────────────

def _make_audit(action: str, device: str | None):
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


def test_audit_site_filter_includes_device_rows_and_deviceless_rows(admin_client):
    site_a = _seed_site(admin_client, "A")
    site_b = _seed_site(admin_client, "B")
    _attach("mock_device", site_a)
    _attach("fail_device", site_b)

    _make_audit("vlan_create", "mock_device")
    _make_audit("vlan_create", "fail_device")
    _make_audit("user_login", None)  # device-unrelated → must remain visible per spec

    r = admin_client.get("/api/v1/audit/", params={"site_id": site_a})
    assert r.status_code == 200
    rows = r.json()
    actions_for_devices = sorted([
        (row["action"], row["device"])
        for row in rows
        if row["action"] in ("vlan_create", "user_login")
    ])
    # site A device-related row + the user_login (no device) — but NOT fail_device's row
    assert ("vlan_create", "mock_device") in actions_for_devices
    assert ("vlan_create", "fail_device") not in actions_for_devices
    assert ("user_login", None) in actions_for_devices


def test_audit_site_filter_empty_site_returns_only_deviceless(admin_client):
    site_id = _seed_site(admin_client, "Empty")
    _make_audit("vlan_create", "mock_device")
    _make_audit("user_login", None)

    r = admin_client.get("/api/v1/audit/", params={"site_id": site_id})
    assert r.status_code == 200
    devs = {row["device"] for row in r.json() if row["action"] in ("vlan_create", "user_login")}
    assert devs == {None}


# ── Regression: existing flows still work ─────────────────────────────────────

def test_devices_listing_still_works(admin_client):
    # Admins bypass scoping by policy → always see all devices regardless of
    # site assignment. (Observer visibility is covered by the RBAC suite.)
    r = admin_client.get("/api/v1/devices/")
    assert r.status_code == 200
    devs = r.json()["data"]
    assert isinstance(devs, list) and len(devs) >= 1


def test_jobs_listing_without_site_filter_unchanged(admin_client):
    _make_job("mock_device")
    r = admin_client.get("/api/v1/jobs/")
    assert r.status_code == 200
    assert r.json()["total"] >= 1


def test_audit_listing_without_site_filter_unchanged(admin_client):
    _make_audit("vlan_create", "mock_device")
    r = admin_client.get("/api/v1/audit/")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
