"""MSP Phase 1 — smoke test: every added ORM column/attribute/table exists.

Runs zero DB work. Just imports the models module and asserts that the
attributes added by Phase 1 are present with the expected shape. If any
attribute is missing we fail fast, before any behavioral test can produce a
misleading traceback.
"""
from sqlalchemy import Boolean, Integer, String

from app.db import models


# ── Columns added to existing tables ─────────────────────────────────────────

def test_site_model_has_kind_column():
    col = models.SiteModel.__table__.columns["kind"]
    assert isinstance(col.type, String)
    assert col.nullable is False


def test_site_model_has_default_group_id_column():
    col = models.SiteModel.__table__.columns["default_group_id"]
    assert isinstance(col.type, Integer)
    # Nullable at DB layer (cyclic FK — see §11.6 Option A).
    assert col.nullable is True


def test_site_model_default_group_relationship_present():
    assert hasattr(models.SiteModel, "default_group")


def test_device_group_model_has_is_default_column():
    col = models.DeviceGroupModel.__table__.columns["is_default"]
    assert isinstance(col.type, Boolean)
    assert col.nullable is False


def test_device_model_has_device_group_id_column():
    col = models.DeviceModel.__table__.columns["device_group_id"]
    assert isinstance(col.type, Integer)
    # MSP: Phase 4 (M3) — flipped NOT NULL. Every device row carries a
    # valid device_group_id (Phase 2 backfilled every pre-existing row).
    assert col.nullable is False


def test_device_model_device_group_relationship_present():
    assert hasattr(models.DeviceModel, "device_group")


def test_user_model_has_is_system_admin_column():
    col = models.UserModel.__table__.columns["is_system_admin"]
    assert isinstance(col.type, Boolean)
    assert col.nullable is False


# ── New table ─────────────────────────────────────────────────────────────────

def test_role_assignment_model_registered():
    assert models.RoleAssignmentModel.__tablename__ == "role_assignments"


def test_role_assignment_columns():
    cols = models.RoleAssignmentModel.__table__.columns
    for name in ("id", "user_id", "site_id", "device_group_id", "role",
                 "created_at", "created_by_user_id"):
        assert name in cols, f"missing column {name}"
    assert cols["device_group_id"].nullable is True
    assert cols["site_id"].nullable is False
    assert cols["user_id"].nullable is False


def test_role_assignment_uniqueness_constraint():
    uniques = [c.name for c in models.RoleAssignmentModel.__table__.constraints
               if hasattr(c, "name") and c.name == "uq_role_assignments_scope"]
    assert uniques, "uq_role_assignments_scope constraint missing"


def test_role_assignment_check_constraint():
    checks = [c.name for c in models.RoleAssignmentModel.__table__.constraints
              if hasattr(c, "name") and c.name == "ck_role_assignments_role"]
    assert checks, "ck_role_assignments_role CHECK missing"
