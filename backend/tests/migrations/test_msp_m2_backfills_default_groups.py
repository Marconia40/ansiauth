"""MSP Phase 2 — Step 2 test.

Seed several REGULAR sites without a default group. Run M2. Assert each
site ended with exactly one ``is_default=TRUE`` group and its
``default_group_id`` points at that group.
"""
import sqlalchemy as sa

from tests.migrations.conftest import (
    REV_M2,
    engine_for,
    seed_site,
    upgrade,
)


def test_step2_creates_default_group_per_regular_site(db_at_m1):
    url = db_at_m1

    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            seed_site(conn, name="Library")
            seed_site(conn, name="Datacenter")
            seed_site(conn, name="Warehouse")
    finally:
        engine.dispose()

    upgrade(url, REV_M2)

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            # Every REGULAR site got a default group.
            rows = conn.execute(sa.text(
                "SELECT s.name, s.default_group_id, g.is_default, g.name "
                "  FROM sites s "
                "  JOIN device_groups g ON g.id = s.default_group_id "
                " WHERE s.kind = 'REGULAR' "
                " ORDER BY s.name"
            )).fetchall()
            assert [(r[0], bool(r[2]), r[3]) for r in rows] == [
                ("Datacenter", True, "Default"),
                ("Library", True, "Default"),
                ("Warehouse", True, "Default"),
            ]
            # No REGULAR site is left with a NULL default_group_id.
            n_null = conn.execute(sa.text(
                "SELECT COUNT(*) FROM sites WHERE kind='REGULAR' AND default_group_id IS NULL"
            )).scalar()
            assert n_null == 0

            # Each site has exactly one is_default=TRUE group.
            per_site = conn.execute(sa.text(
                "SELECT s.name, COUNT(*) "
                "  FROM sites s JOIN device_groups g ON g.site_id = s.id "
                " WHERE s.kind='REGULAR' AND g.is_default = 1 "
                " GROUP BY s.name"
            )).fetchall()
            assert all(c == 1 for _, c in per_site), per_site
    finally:
        engine.dispose()


def test_step2_promotes_existing_default_named_group(db_at_m1):
    """D6 collision handling — a user-created group already named 'Default'
    in a REGULAR site is *promoted* (is_default flipped to TRUE) rather
    than inserted alongside, so the eventual per-site unique (site_id, name)
    constraint added in Phase 4 doesn't collide.
    """
    url = db_at_m1

    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            site_id = seed_site(conn, name="Library")
            # A user-created group happens to already be named 'Default'.
            conn.execute(sa.text(
                "INSERT INTO device_groups (name, site_id, is_default, created_at) "
                "VALUES ('Default', :s, 0, CURRENT_TIMESTAMP)"
            ).bindparams(s=site_id))
    finally:
        engine.dispose()

    upgrade(url, REV_M2)

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT id, is_default FROM device_groups "
                " WHERE name='Default' AND site_id = "
                "       (SELECT id FROM sites WHERE name='Library')"
            )).fetchall()
            # Exactly one 'Default' group — the pre-existing one, promoted.
            assert len(rows) == 1
            assert bool(rows[0][1]) is True

            # Site now points at it.
            default_gid = conn.execute(sa.text(
                "SELECT default_group_id FROM sites WHERE name='Library'"
            )).scalar()
            assert default_gid == rows[0][0]
    finally:
        engine.dispose()
