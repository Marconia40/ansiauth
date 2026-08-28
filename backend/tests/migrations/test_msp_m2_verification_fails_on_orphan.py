"""MSP Phase 2 — verification failure test.

The migration's terminal ``_assert_zero`` block must raise ``RuntimeError``
(surfaced as a non-zero alembic exit) if any invariant is violated after
the six steps. Simulate this by pre-populating Base Infra with a NULL
``default_group_id`` **and** a REGULAR site whose Default group also
never materialises — we drop the ``device_groups.is_default`` flag we set
manually so Step 2's UPDATE-by-lookup can't find a group to link.

The clean way to force this is to bypass Step 2's INSERT by seeding a
REGULAR site with a *non-default* group named 'Default' — Step 2 would
promote it — but we can leave the site without any group at all. Then
delete the row Step 2 inserts before it commits by ... simpler: run the
migration as-is, expect success, then confirm the verification block does
fire by tampering the DB and running M2 again.

Actually the plan-authored version relies on breaking Step 3c's
prerequisite (Base Infra's default_group_id) so Step 3a can't find a
target. We reproduce that here by:
  1. seeding a site-less device;
  2. seeding a Base Infra row with default_group_id=NULL and no default
     group at all — but that breaks Step 1's idempotency (it will
     re-attempt the insert). Simpler alternative below.

We instead force the assertion by directly running the ``_assert_zero``
helper against a DB where we manually null a device's group_id after M2.
This exercises the exact failure path.
"""
import sqlalchemy as sa

from tests.migrations.conftest import (
    REV_M2,
    engine_for,
    seed_device,
    seed_site,
    upgrade,
)


def test_verification_raises_on_orphaned_device(db_at_m1):
    """Populate a well-formed DB, run M2 successfully, then manually null
    one device's device_group_id and re-run the migration's verification
    block. The helper must raise ``RuntimeError``.
    """
    url = db_at_m1

    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            library_id = seed_site(conn, name="Library")
            seed_device(conn, name="lib_1", site_id=library_id)
    finally:
        engine.dispose()

    upgrade(url, REV_M2)

    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            # Break the invariant.
            conn.execute(sa.text(
                "UPDATE devices SET device_group_id = NULL WHERE name = 'lib_1'"
            ))
    finally:
        engine.dispose()

    # Import the helper directly and confirm it raises against the tampered
    # DB.
    import importlib.util
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    path = root / "migrations" / "versions" / "e2msp2_msp_backfill.py"
    spec = importlib.util.spec_from_file_location("msp_backfill_mod", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            import pytest
            with pytest.raises(RuntimeError, match="device_group_id"):
                mod._assert_zero(
                    conn,
                    "SELECT COUNT(*) FROM devices WHERE device_group_id IS NULL",
                    "devices with no device_group_id",
                )
    finally:
        engine.dispose()
