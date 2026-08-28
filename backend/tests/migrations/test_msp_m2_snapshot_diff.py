"""MSP Phase 2 — T2.2 snapshot-diff test (R3 mitigation).

For every seeded operator / observer user, verify the visibility set
computed via the legacy ``user_allowed_sites`` model is identical to the
one computed via the new ``role_assignments`` model post-migration.

Semantics:
  * Legacy path (pre-M2): a device is visible when
    ``device.site_id ∈ user.allowed_sites``. Devices with
    ``site_id IS NULL`` fall back into scope only if the user has an
    empty ``allowed_sites`` set.
  * MSP path (post-M2): a device is visible when its group's site is
    covered by a ``role_assignments`` row for the user. ``operator`` /
    ``observer`` grants land at site scope (``device_group_id IS NULL``);
    per-group grants are Phase 3 material and don't apply here.

D14 correction: the M2 backfill deliberately does not migrate the legacy
``site_id IS NULL`` fallback. Users who relied on it end up seeing zero
devices under the MSP model, which is the intended new behaviour. This
test asserts equality only for users with at least one ``allowed_sites``
row; users on the fallback path are represented separately.
"""
import sqlalchemy as sa

from tests.migrations.conftest import (
    REV_M2,
    engine_for,
    seed_allowed_site,
    seed_device,
    seed_site,
    seed_user,
    upgrade,
)


def _legacy_visible(conn, user_id: int) -> set[str]:
    allowed_ids = {
        r[0] for r in conn.execute(sa.text(
            "SELECT site_id FROM user_allowed_sites WHERE user_id = :u"
        ).bindparams(u=user_id)).fetchall()
    }
    if not allowed_ids:
        rows = conn.execute(sa.text(
            "SELECT name FROM devices WHERE site_id IS NULL"
        )).fetchall()
    else:
        placeholders = ",".join(f":s{i}" for i in range(len(allowed_ids)))
        bind = {f"s{i}": sid for i, sid in enumerate(allowed_ids)}
        rows = conn.execute(sa.text(
            f"SELECT name FROM devices WHERE site_id IN ({placeholders})"
        ).bindparams(**bind)).fetchall()
    return {r[0] for r in rows}


def _msp_visible(conn, user_id: int) -> set[str]:
    """Devices visible via role_assignments after M2.

    Site-scoped grant → every device in that site. We resolve devices via
    the new ``devices.device_group_id → device_groups.site_id`` path (the
    denormalised ``devices.site_id`` still exists here but is dropped in
    Phase 5).
    """
    rows = conn.execute(sa.text("""
        SELECT DISTINCT d.name
          FROM devices d
          JOIN device_groups g ON g.id = d.device_group_id
          JOIN role_assignments ra ON ra.site_id = g.site_id
         WHERE ra.user_id = :u
           AND ra.device_group_id IS NULL
    """).bindparams(u=user_id)).fetchall()
    return {r[0] for r in rows}


def test_snapshot_diff_matches_for_users_with_allowed_sites(db_at_m1):
    url = db_at_m1

    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            library_id = seed_site(conn, name="Library")
            dc_id = seed_site(conn, name="Datacenter")
            warehouse_id = seed_site(conn, name="Warehouse")

            seed_device(conn, name="lib_1", site_id=library_id)
            seed_device(conn, name="lib_2", site_id=library_id)
            seed_device(conn, name="dc_1", site_id=dc_id)
            seed_device(conn, name="dc_2", site_id=dc_id)
            seed_device(conn, name="wh_1", site_id=warehouse_id)

            # Observer scoped to Library only.
            obs_id = seed_user(conn, username="juan", role="observer")
            seed_allowed_site(conn, user_id=obs_id, site_id=library_id)

            # Operator scoped to Library + Datacenter.
            op_id = seed_user(conn, username="rita", role="operator")
            seed_allowed_site(conn, user_id=op_id, site_id=library_id)
            seed_allowed_site(conn, user_id=op_id, site_id=dc_id)

            # Observer scoped to Warehouse (a site with a single device).
            wh_obs_id = seed_user(conn, username="dana", role="observer")
            seed_allowed_site(conn, user_id=wh_obs_id, site_id=warehouse_id)
    finally:
        engine.dispose()

    # Snapshot legacy visibility *before* running M2.
    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            legacy = {
                uid: _legacy_visible(conn, uid)
                for uid in (obs_id, op_id, wh_obs_id)
            }
    finally:
        engine.dispose()

    upgrade(url, REV_M2)

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            new = {
                uid: _msp_visible(conn, uid)
                for uid in (obs_id, op_id, wh_obs_id)
            }
    finally:
        engine.dispose()

    for uid in (obs_id, op_id, wh_obs_id):
        assert legacy[uid] == new[uid], (
            f"visibility mismatch for user_id={uid}: "
            f"legacy={legacy[uid]}, new={new[uid]}"
        )


def test_snapshot_diff_users_on_fallback_lose_visibility(db_at_m1):
    """D14 correction: users with zero allowed_sites relied on the
    ``site_id IS NULL`` fallback and now see nothing under MSP. The
    post-migration report (§4.3 item 2) captures them; this test just
    documents the expected new state so a future accidental re-instatement
    of the fallback would fail loudly.
    """
    url = db_at_m1

    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            seed_device(conn, name="unassigned_1", site_id=None)
            seed_device(conn, name="unassigned_2", site_id=None)
            drifter_id = seed_user(conn, username="drifter", role="observer")
            # No seed_allowed_site() call — drifter is on the fallback path.
    finally:
        engine.dispose()

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            legacy = _legacy_visible(conn, drifter_id)
            # Legacy fallback: drifter sees the two site-less devices.
            assert legacy == {"unassigned_1", "unassigned_2"}
    finally:
        engine.dispose()

    upgrade(url, REV_M2)

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            new = _msp_visible(conn, drifter_id)
            # Post-M2: zero grants → zero visibility.
            assert new == set()

            # And drifter did not accidentally receive is_system_admin.
            flag = conn.execute(sa.text(
                "SELECT is_system_admin FROM users WHERE id = :u"
            ).bindparams(u=drifter_id)).scalar()
            assert bool(flag) is False
    finally:
        engine.dispose()
