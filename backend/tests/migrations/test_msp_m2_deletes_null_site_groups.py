"""MSP Phase 2 — Step 5 test (D21a).

Any legacy ``device_groups`` row whose ``site_id`` is NULL is data-lossy
under the MSP model — no site owns it, no scope grants apply. Step 5
deletes such rows and their member associations. It runs *after* Step 3
so live devices have already been redirected to a valid group.
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


def test_step5_drops_null_site_groups_and_their_members(db_at_m1):
    url = db_at_m1

    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            # A legacy group with no site + one member. The member device is
            # site-less so Step 3a will hand it to Base Infra's Default before
            # Step 5 deletes the group.
            seed_device(conn, name="legacy_device", site_id=None)
            ghost_gid = seed_device_group(conn, name="Ghost", site_id=None)
            seed_group_member(
                conn, group_id=ghost_gid, device_name="legacy_device"
            )

            # A well-formed comparison group that should survive untouched.
            library_id = seed_site(conn, name="Library")
            keeper_gid = seed_device_group(
                conn, name="Keeper", site_id=library_id
            )
    finally:
        engine.dispose()

    upgrade(url, REV_M2)

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            # Ghost group is gone.
            n_ghost = conn.execute(sa.text(
                "SELECT COUNT(*) FROM device_groups WHERE id = :g"
            ).bindparams(g=ghost_gid)).scalar()
            assert n_ghost == 0

            # Its membership row is gone.
            n_members = conn.execute(sa.text(
                "SELECT COUNT(*) FROM device_group_members WHERE group_id = :g"
            ).bindparams(g=ghost_gid)).scalar()
            assert n_members == 0

            # The well-formed group survived.
            n_keeper = conn.execute(sa.text(
                "SELECT COUNT(*) FROM device_groups WHERE id = :g"
            ).bindparams(g=keeper_gid)).scalar()
            assert n_keeper == 1

            # And no NULL-site group exists anywhere after M2.
            n_null_site = conn.execute(sa.text(
                "SELECT COUNT(*) FROM device_groups WHERE site_id IS NULL"
            )).scalar()
            assert n_null_site == 0
    finally:
        engine.dispose()
