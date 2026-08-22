"""MSP Phase 5 (M4) — Alembic upgrade drops every legacy artefact.

Runs Alembic in a subprocess against an isolated scratch SQLite DB (same
pattern as ``test_msp_m1_roundtrip``) so the parent test session's
``DATABASE_URL`` is not disturbed. Asserts that after ``upgrade e5msp4_cleanup``:

  * ``devices.site_id`` column is gone (and its FK / index with it).
  * ``device_group_members`` table is gone.
  * ``user_allowed_sites`` table is gone.
  * ``users.role`` column is gone.
  * Every Phase-4 constraint installed by M3 is still in place — so M4 is a
    pure drop and does not accidentally regress the guarantees it built on.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import sqlalchemy as sa


REVISION = "e5msp4_cleanup"
PARENT_REVISION = "e4msp3_enforce"

BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


def _alembic(url: str, *args: str) -> None:
    """Invoke alembic in a subprocess with DATABASE_URL scoped to ``url``."""
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env.setdefault("JWT_SECRET_KEY", "test_jwt_secret_key_not_for_production")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(ALEMBIC_INI), *args],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed: {result.stderr}\n{result.stdout}"
    )


@pytest.fixture
def scratch_db():
    fd, path = tempfile.mkstemp(prefix="msp_m4_drops_", suffix=".db")
    os.close(fd)
    url = f"sqlite:///{path}"
    yield url
    if os.path.exists(path):
        os.remove(path)


def _column_names(engine, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns(table)}


def _table_names(engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _index_names(engine, table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(engine).get_indexes(table)}


def _uniques(engine, table: str) -> set[str]:
    return {
        uc["name"]
        for uc in sa.inspect(engine).get_unique_constraints(table)
        if uc.get("name")
    }


def test_upgrade_removes_every_legacy_artefact(scratch_db):
    url = scratch_db

    # 1. Bring the scratch DB to the revision *before* M4 (i.e. post-Phase-4).
    _alembic(url, "upgrade", PARENT_REVISION)

    engine = sa.create_engine(url)
    try:
        # Pre-condition sanity: everything M4 will drop must currently exist.
        assert "site_id" in _column_names(engine, "devices")
        assert "device_group_members" in _table_names(engine)
        assert "user_allowed_sites" in _table_names(engine)
        assert "role" in _column_names(engine, "users")
    finally:
        engine.dispose()

    # 2. Apply M4.
    _alembic(url, "upgrade", REVISION)

    engine = sa.create_engine(url)
    try:
        tables = _table_names(engine)
        devices_cols = _column_names(engine, "devices")
        users_cols = _column_names(engine, "users")

        # § legacy drops
        assert "site_id" not in devices_cols, (
            "devices.site_id must be gone after M4"
        )
        assert "device_group_members" not in tables, (
            "device_group_members table must be gone after M4"
        )
        assert "user_allowed_sites" not in tables, (
            "user_allowed_sites table must be gone after M4"
        )
        assert "role" not in users_cols, (
            "users.role must be gone after M4"
        )

        # § the associated FK / index on devices.site_id disappears with the
        # column — verify the index name specifically.
        assert "ix_devices_site_id" not in _index_names(engine, "devices")

        # § M3 (Phase 4) invariants survive — M4 is drop-only.
        dg_cols = _column_names(engine, "device_groups")
        assert "site_id" in dg_cols
        assert "is_default" in dg_cols

        # NOT NULL flips M3 installed.
        devices_meta = {
            c["name"]: c for c in sa.inspect(engine).get_columns("devices")
        }
        assert devices_meta["device_group_id"]["nullable"] is False, (
            "M3's devices.device_group_id NOT NULL must survive M4"
        )
        dg_meta = {
            c["name"]: c for c in sa.inspect(engine).get_columns("device_groups")
        }
        assert dg_meta["site_id"]["nullable"] is False, (
            "M3's device_groups.site_id NOT NULL must survive M4"
        )

        # Per-site UNIQUE(site_id, name) added by M3.
        assert "uq_device_group_site_name" in _uniques(engine, "device_groups")

        # Partial unique indexes M3 added — SQLite exposes them via
        # get_indexes with unique=True.
        dg_indexes = {
            ix["name"]: ix for ix in sa.inspect(engine).get_indexes("device_groups")
        }
        assert "ux_device_groups_one_default_per_site" in dg_indexes
        assert dg_indexes["ux_device_groups_one_default_per_site"]["unique"]

        sites_indexes = {
            ix["name"]: ix for ix in sa.inspect(engine).get_indexes("sites")
        }
        assert "ux_sites_single_base_infra" in sites_indexes
        assert sites_indexes["ux_sites_single_base_infra"]["unique"]

        # role_assignments and is_system_admin (Phase 1) still present.
        assert "role_assignments" in tables
        assert "is_system_admin" in users_cols
    finally:
        engine.dispose()


def test_downgrade_recreates_empty_structures(scratch_db):
    """The downgrade cannot restore data but MUST restore the schema shape so
    the alembic chain is walkable in both directions. Verify the empty
    structures come back."""
    url = scratch_db

    _alembic(url, "upgrade", REVISION)
    _alembic(url, "downgrade", PARENT_REVISION)

    engine = sa.create_engine(url)
    try:
        tables = _table_names(engine)
        assert "device_group_members" in tables
        assert "user_allowed_sites" in tables
        assert "site_id" in _column_names(engine, "devices")
        assert "role" in _column_names(engine, "users")
        assert "ix_devices_site_id" in _index_names(engine, "devices")
    finally:
        engine.dispose()
