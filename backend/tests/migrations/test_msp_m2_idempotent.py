"""MSP Phase 2 — idempotency test.

Running M2 twice must land the DB in the exact same state as running it
once. Verified by snapshotting counts of every table M2 touches after the
first upgrade, running M2's SQL block a second time via a raw connection
(alembic itself won't re-run a completed revision), then re-snapshotting
and asserting equality.
"""
import importlib.util
import sys
from pathlib import Path

import sqlalchemy as sa

from tests.migrations.conftest import (
    REV_M2,
    engine_for,
    seed_allowed_site,
    seed_device,
    seed_device_group,
    seed_group_member,
    seed_site,
    seed_user,
    upgrade,
)


TABLES_SNAPSHOT = (
    "sites",
    "device_groups",
    "devices",
    "device_group_members",
    "users",
    "user_allowed_sites",
    "role_assignments",
    "audit_logs",
)


def _snapshot(conn) -> dict[str, int]:
    return {
        t: conn.execute(sa.text(f"SELECT COUNT(*) FROM {t}")).scalar()
        for t in TABLES_SNAPSHOT
    }


def _load_migration_module():
    """Import the M2 migration file as a module so we can invoke ``upgrade``
    against a live connection outside of Alembic's runner."""
    root = Path(__file__).resolve().parents[2]
    path = root / "migrations" / "versions" / "e2msp2_msp_backfill.py"
    spec = importlib.util.spec_from_file_location("msp_backfill_mod", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_running_m2_twice_leaves_state_identical(db_at_m1):
    url = db_at_m1

    # Diverse seed: one of every category so every step has work to do.
    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            library_id = seed_site(conn, name="Library")
            gid = seed_device_group(conn, name="Circulation", site_id=library_id)
            seed_device(conn, name="lib_1", site_id=library_id)
            seed_group_member(conn, group_id=gid, device_name="lib_1")

            seed_device(conn, name="orphan", site_id=None)

            admin_id = seed_user(conn, username="alice", role="admin")
            obs_id = seed_user(conn, username="juan", role="observer")
            seed_allowed_site(conn, user_id=obs_id, site_id=library_id)
    finally:
        engine.dispose()

    upgrade(url, REV_M2)

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            first = _snapshot(conn)
    finally:
        engine.dispose()

    # Re-run the migration's upgrade() body directly. Alembic won't repeat
    # a completed revision, but the SQL itself must remain a no-op.
    mig = _load_migration_module()
    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            # ``op.get_bind()`` inside the migration reads the ambient
            # Alembic bind; we simulate that with a MigrationContext.
            from alembic.runtime.migration import MigrationContext
            ctx = MigrationContext.configure(conn)
            from alembic.operations import Operations
            ops = Operations(ctx)
            # Rebind op module-level shim for the duration of this call.
            import alembic.op as alembic_op
            saved_get_bind = alembic_op.get_bind
            saved_execute = alembic_op.execute
            alembic_op.get_bind = ops.get_bind
            alembic_op.execute = ops.execute
            try:
                mig.upgrade()
            finally:
                alembic_op.get_bind = saved_get_bind
                alembic_op.execute = saved_execute
    finally:
        engine.dispose()

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            second = _snapshot(conn)
    finally:
        engine.dispose()

    assert first == second, (
        f"M2 is not idempotent — table row counts differ after second run:\n"
        f"first:  {first}\nsecond: {second}"
    )
    # Anti-regression: alice and juan didn't accidentally get a duplicate row.
    _ = admin_id  # kept for readability; not asserted (covered by snapshot)
