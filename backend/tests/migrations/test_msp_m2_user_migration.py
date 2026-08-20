"""MSP Phase 2 — Step 6 test (D24 + D14 answers).

  * 6a — every ``admin`` / ``super-admin`` user is promoted to
    ``is_system_admin=TRUE``.
  * 6b — every ``operator`` / ``observer`` × ``allowed_sites`` pair
    yields one site-scoped ``role_assignments`` row.
  * 6c — deliberately NOT executed. Users with an empty ``allowed_sites``
    set previously matched the ``site_id IS NULL`` fallback; under D14
    they receive **no** grant (Base Infra is is_system_admin-only). The
    post-migration report captures them for operator review.
"""
import sqlalchemy as sa

from tests.migrations.conftest import (
    REV_M2,
    engine_for,
    seed_allowed_site,
    seed_site,
    seed_user,
    upgrade,
)


def test_step6_migrates_users_correctly(db_at_m1):
    url = db_at_m1

    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            # Two REGULAR sites.
            library_id = seed_site(conn, name="Library")
            dc_id = seed_site(conn, name="Datacenter")

            # 6a fixtures.
            admin_id = seed_user(conn, username="alice", role="admin")
            super_id = seed_user(conn, username="bob", role="super-admin")

            # 6b fixture: observer scoped to Library + Datacenter.
            obs_id = seed_user(conn, username="juan", role="observer")
            seed_allowed_site(conn, user_id=obs_id, site_id=library_id)
            seed_allowed_site(conn, user_id=obs_id, site_id=dc_id)

            # D14 fixture: operator with no allowed_sites at all. Under the
            # legacy fallback they would have matched site_id IS NULL. Under
            # the MSP model they receive zero grants and land on the report.
            unassigned_op_id = seed_user(
                conn, username="drifter", role="operator"
            )
    finally:
        engine.dispose()

    upgrade(url, REV_M2)

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            # 6a — both admins ended up is_system_admin=TRUE.
            for uid in (admin_id, super_id):
                flag = conn.execute(sa.text(
                    "SELECT is_system_admin FROM users WHERE id = :u"
                ).bindparams(u=uid)).scalar()
                assert bool(flag) is True

            # 6b — juan has one site-wide grant per allowed site, both
            # roles matching the user's global role, and no group-level
            # grants.
            juan_grants = conn.execute(sa.text("""
                SELECT site_id, device_group_id, role
                  FROM role_assignments
                 WHERE user_id = :u
                 ORDER BY site_id
            """).bindparams(u=obs_id)).fetchall()
            assert sorted((s, g, r) for s, g, r in juan_grants) == sorted([
                (library_id, None, "observer"),
                (dc_id, None, "observer"),
            ])

            # 6c — drifter has zero grants.
            n_drifter_grants = conn.execute(sa.text(
                "SELECT COUNT(*) FROM role_assignments WHERE user_id = :u"
            ).bindparams(u=unassigned_op_id)).scalar()
            assert n_drifter_grants == 0

            # 6c — drifter is not is_system_admin either.
            drifter_flag = conn.execute(sa.text(
                "SELECT is_system_admin FROM users WHERE id = :u"
            ).bindparams(u=unassigned_op_id)).scalar()
            assert bool(drifter_flag) is False
    finally:
        engine.dispose()


def test_step6_skips_base_infra_grants(db_at_m1):
    """Even if a user has an allowed_sites row pointing at Base Infra
    (would only happen in bizarre pre-migration states), Step 6b joins on
    ``s.kind = 'REGULAR'`` so no Base-Infra grant is ever created."""
    url = db_at_m1

    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            # Pre-create a Base Infra site + a REGULAR peer.
            base_id = seed_site(
                conn, name="Base Infrastructure", kind="BASE_INFRASTRUCTURE"
            )
            regular_id = seed_site(conn, name="Library")

            uid = seed_user(conn, username="mystery", role="observer")
            seed_allowed_site(conn, user_id=uid, site_id=base_id)
            seed_allowed_site(conn, user_id=uid, site_id=regular_id)
    finally:
        engine.dispose()

    upgrade(url, REV_M2)

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(sa.text(
                "SELECT site_id FROM role_assignments WHERE user_id = :u"
            ).bindparams(u=uid)).fetchall()
            grant_site_ids = {r[0] for r in rows}
            assert grant_site_ids == {regular_id}
            assert base_id not in grant_site_ids
    finally:
        engine.dispose()
