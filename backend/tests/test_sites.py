"""Tests for the Site CRUD API (step 7.1)."""
import pytest

from app.db.models import DeviceModel, SiteModel
from app.db.session import get_session


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_sites():
    """Wipe sites + dependent tables around every test.

    MSP: Phase 4 — SQLite doesn't enforce FK RESTRICT by default, so simply
    deleting from ``sites`` leaves orphans in ``device_groups`` /
    ``device_group_members`` / ``role_assignments`` that still carry
    ``site_id``. Under M3 the partial unique index
    ``ux_device_groups_one_default_per_site`` then blocks the next site
    create (whose row.id can recycle to 1). Wipe every dependent row too,
    null out the mock devices' authoritative FKs so
    ``ensure_base_infrastructure`` can rebuild cleanly, and reseed both.
    """
    from app.db.models import (
        DeviceGroupModel,
        RoleAssignmentModel,
    )
    from app.composition import inventory, site_repository
    from app.core.exceptions import ValidationError
    from app.repositories.site_repository import BASE_INFRA_SITE_KIND

    def _wipe_and_reseed():
        with get_session() as session:
            # devices.device_group_id is NOT NULL — delete devices outright
            # so the reseed below can re-create them inside Mock Site's Default.
            session.query(DeviceModel).delete(synchronize_session=False)
            session.query(RoleAssignmentModel).delete(synchronize_session=False)
            # Null sites.default_group_id first so RESTRICT doesn't block the
            # group delete.
            session.query(SiteModel).update(
                {SiteModel.default_group_id: None}, synchronize_session=False,
            )
            session.query(DeviceGroupModel).delete(synchronize_session=False)
            session.query(SiteModel).delete(synchronize_session=False)
        # site_service.ensure_base_infrastructure()/device_service.seed_defaults()
        # were deleted by the migration to FINAL_ARCHITECTURE.md --
        # SiteRepository.crear_con_grupo_default() + Inventory.register() are
        # the modern replacements. Mirrors device_service.seed_defaults()'s
        # exact behavior: "mock_device"/"fail_device" live in a REGULAR
        # "Mock Site" (not Base-Infra, which is hidden from non-system-admins
        # per D14) so _attach_device() below has a real seed device to move.
        site_repository.crear_con_grupo_default(
            "Base Infrastructure", "System-managed base infrastructure site.",
            kind=BASE_INFRA_SITE_KIND,
        )
        mock_site = site_repository.crear_con_grupo_default("Mock Site", kind="REGULAR")
        for name, host in (("mock_device", "192.168.1.1"), ("fail_device", "192.168.1.2")):
            try:
                inventory.register(
                    name=name, host=host, vendor="cisco_ios", platform="ios",
                    username="admin", password="admin",
                    site_id=mock_site.id, device_group_id=mock_site.default_group_id,
                    actor={"username": "admin"},
                )
            except ValidationError:
                pass

    _wipe_and_reseed()
    yield
    _wipe_and_reseed()


def _attach_device(site_id: int, name: str = "mock_device"):
    """Bind an existing seed device to a site (via the site's Default group)
    for delete-conflict / device-count scenarios."""
    from app.db.models import DeviceGroupModel, SiteModel as _Site
    with get_session() as session:
        default_group_id = (
            session.query(_Site.default_group_id).filter_by(id=site_id).scalar()
        )
        assert default_group_id is not None, (
            f"site {site_id} has no default group — create_site must run first"
        )
        dev = session.query(DeviceModel).filter_by(name=name).first()
        assert dev is not None, f"seed device '{name}' missing — check device_service.seed_defaults"
        dev.device_group_id = default_group_id


# ── CRUD ──────────────────────────────────────────────────────────────────────

def test_create_site_returns_201_payload_shape(admin_client):
    r = admin_client.post("/api/v1/sites/", json={"name": "Library", "description": "Library closets"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    site = body["data"]
    assert site["name"] == "Library"
    assert site["description"] == "Library closets"
    assert site["device_count"] == 0
    assert "id" in site and "created_at" in site and "updated_at" in site


def test_create_trims_whitespace(admin_client):
    r = admin_client.post("/api/v1/sites/", json={"name": "  HQ  "})
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "HQ"


def test_create_empty_name_rejected(admin_client):
    r = admin_client.post("/api/v1/sites/", json={"name": "   "})
    assert r.status_code in (400, 422), r.text


def test_create_duplicate_name_rejected(admin_client):
    r = admin_client.post("/api/v1/sites/", json={"name": "Datacenter"})
    assert r.status_code == 200
    r = admin_client.post("/api/v1/sites/", json={"name": "Datacenter"})
    # api/main.py's ValidationError handler now maps to 422, not 400 --
    # verified across the whole app (app/main.py:validation_error_handler).
    assert r.status_code == 422, r.text


def test_list_returns_sites_sorted_by_name(admin_client):
    for name in ("HQ", "Library", "Datacenter"):
        admin_client.post("/api/v1/sites/", json={"name": name})
    r = admin_client.get("/api/v1/sites/")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()["data"]]
    assert names == sorted(names)


def test_get_site_by_id(admin_client):
    created = admin_client.post("/api/v1/sites/", json={"name": "Laboratory"}).json()["data"]
    r = admin_client.get(f"/api/v1/sites/{created['id']}")
    assert r.status_code == 200
    assert r.json()["data"]["name"] == "Laboratory"


def test_get_site_not_found(admin_client):
    r = admin_client.get("/api/v1/sites/999999")
    assert r.status_code == 404


def test_update_site_name_and_description(admin_client):
    created = admin_client.post("/api/v1/sites/", json={"name": "Old"}).json()["data"]
    r = admin_client.put(f"/api/v1/sites/{created['id']}", json={"name": "New", "description": "renamed"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["name"] == "New"
    assert data["description"] == "renamed"


def test_update_no_fields_returns_400(admin_client):
    created = admin_client.post("/api/v1/sites/", json={"name": "A"}).json()["data"]
    r = admin_client.put(f"/api/v1/sites/{created['id']}", json={})
    # ValidationError -> 422 now (see test_create_duplicate_name_rejected).
    assert r.status_code == 422


def test_update_site_duplicate_name_rejected(admin_client):
    a = admin_client.post("/api/v1/sites/", json={"name": "A"}).json()["data"]
    admin_client.post("/api/v1/sites/", json={"name": "B"})
    r = admin_client.put(f"/api/v1/sites/{a['id']}", json={"name": "B"})
    assert r.status_code == 422


def test_update_missing_site_returns_404(admin_client):
    r = admin_client.put("/api/v1/sites/999999", json={"name": "X"})
    assert r.status_code == 404


def test_delete_empty_site(admin_client):
    created = admin_client.post("/api/v1/sites/", json={"name": "ToDelete"}).json()["data"]
    r = admin_client.delete(f"/api/v1/sites/{created['id']}")
    assert r.status_code == 200
    assert r.json()["data"]["site_id"] == created["id"]


def test_delete_missing_site_returns_404(admin_client):
    r = admin_client.delete("/api/v1/sites/999999")
    assert r.status_code == 404


def test_delete_site_with_devices_returns_409(admin_client):
    site = admin_client.post("/api/v1/sites/", json={"name": "Occupied"}).json()["data"]
    _attach_device(site["id"])
    r = admin_client.delete(f"/api/v1/sites/{site['id']}")
    assert r.status_code == 409, r.text
    body = r.json()
    assert body.get("error_code") == "CONFLICT"
    # Device must NOT have been cascade-deleted.
    with get_session() as session:
        assert session.query(DeviceModel).filter_by(name="mock_device").first() is not None


def test_device_count_reflects_attached_devices(admin_client):
    site = admin_client.post("/api/v1/sites/", json={"name": "Counts"}).json()["data"]
    _attach_device(site["id"])
    r = admin_client.get(f"/api/v1/sites/{site['id']}")
    assert r.status_code == 200
    assert r.json()["data"]["device_count"] == 1


# ── RBAC ──────────────────────────────────────────────────────────────────────

def test_observer_can_list(observer_client):
    r = observer_client.get("/api/v1/sites/")
    assert r.status_code == 200


def test_observer_cannot_create(observer_client):
    r = observer_client.post("/api/v1/sites/", json={"name": "Nope"})
    assert r.status_code == 403


def test_observer_cannot_delete(observer_client, admin_client):
    site = admin_client.post("/api/v1/sites/", json={"name": "X"}).json()["data"]
    r = observer_client.delete(f"/api/v1/sites/{site['id']}")
    assert r.status_code == 403


# ── Regression: existing device flows ────────────────────────────────────────

def test_existing_device_list_unaffected(observer_client):
    """site_id column must not break the legacy /devices listing."""
    r = observer_client.get("/api/v1/devices/")
    assert r.status_code == 200
    devs = r.json()["data"]
    assert isinstance(devs, list)
    # site_id is internal — it's allowed to leak via the public schema or not,
    # but the existing public fields must still be present.
    if devs:
        for required in ("id", "name", "host", "vendor", "platform", "username"):
            assert required in devs[0], f"missing field {required}"
