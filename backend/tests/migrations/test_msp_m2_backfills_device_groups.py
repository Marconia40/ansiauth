"""MSP Phase 2 — Step 3 test.

Three device categories, one row each:
  * 3a: site-less device → Base Infra's Default group.
  * 3b: device with exactly one group inside its site → that group.
  * 3c: device with zero groups inside its site → the site's Default group.
"""
import sqlalchemy as sa

from tests.migrations.conftest import (
    REV_M2,
    engine_for,
    seed_device,
    seed_device_group,
    seed_group_member,
    seed_site,
    upgrade,
)


def test_step3_assigns_every_device_to_a_group(db_at_m1):
    url = db_at_m1

    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            # 3a — site-less device.
            seed_device(conn, name="orphan_device", site_id=None)

            # 3b — device with exactly one group inside its site.
            library_id = seed_site(conn, name="Library")
            circulation_gid = seed_device_group(
                conn, name="Circulation", site_id=library_id
            )
            seed_device(conn, name="lib_switch_1", site_id=library_id)
            seed_group_member(
                conn, group_id=circulation_gid, device_name="lib_switch_1"
            )

            # 3c — device in a site that has no user-created group at all.
            dc_id = seed_site(conn, name="Datacenter")
            seed_device(conn, name="dc_switch_1", site_id=dc_id)
    finally:
        engine.dispose()

    upgrade(url, REV_M2)

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            # 3a — orphan device points at Base Infra's default group.
            base_default_gid = conn.execute(sa.text("""
                SELECT default_group_id FROM sites WHERE kind='BASE_INFRASTRUCTURE'
            """)).scalar()
            assert base_default_gid is not None
            orphan_gid = conn.execute(sa.text(
                "SELECT device_group_id FROM devices WHERE name='orphan_device'"
            )).scalar()
            assert orphan_gid == base_default_gid

            # 3b — device with a single in-site group lands in that group.
            lib_gid = conn.execute(sa.text(
                "SELECT device_group_id FROM devices WHERE name='lib_switch_1'"
            )).scalar()
            assert lib_gid == circulation_gid

            # 3c — device in a site with no user group lands in the site's default.
            dc_default_gid = conn.execute(sa.text("""
                SELECT s.default_group_id FROM sites s WHERE s.name='Datacenter'
            """)).scalar()
            assert dc_default_gid is not None
            dc_gid = conn.execute(sa.text(
                "SELECT device_group_id FROM devices WHERE name='dc_switch_1'"
            )).scalar()
            assert dc_gid == dc_default_gid

            # No device left with NULL device_group_id anywhere.
            n_null = conn.execute(sa.text(
                "SELECT COUNT(*) FROM devices WHERE device_group_id IS NULL"
            )).scalar()
            assert n_null == 0
    finally:
        engine.dispose()
