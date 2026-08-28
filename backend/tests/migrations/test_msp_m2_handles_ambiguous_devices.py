"""MSP Phase 2 — Step 4 test (R1 mitigation).

A device that is a member of *multiple* groups within its site cannot be
unambiguously assigned to one of them. Step 3c catches these and routes
them to the site's Default group. Step 4 emits one immutable audit row
per such device recording the pre-migration group set.
"""
import json

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


def test_step4_routes_ambiguous_device_to_default_and_audits(db_at_m1):
    url = db_at_m1

    engine = engine_for(url)
    try:
        with engine.begin() as conn:
            library_id = seed_site(conn, name="Library")
            circulation_gid = seed_device_group(
                conn, name="Circulation", site_id=library_id
            )
            reference_gid = seed_device_group(
                conn, name="Reference", site_id=library_id
            )
            seed_device(conn, name="lib_switch_1", site_id=library_id)
            # In *two* groups inside the same site — ambiguous.
            seed_group_member(
                conn, group_id=circulation_gid, device_name="lib_switch_1"
            )
            seed_group_member(
                conn, group_id=reference_gid, device_name="lib_switch_1"
            )
    finally:
        engine.dispose()

    upgrade(url, REV_M2)

    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            # Device landed in the site's Default group.
            default_gid = conn.execute(sa.text(
                "SELECT default_group_id FROM sites WHERE name='Library'"
            )).scalar()
            assigned_gid = conn.execute(sa.text(
                "SELECT device_group_id FROM devices WHERE name='lib_switch_1'"
            )).scalar()
            assert assigned_gid == default_gid

            # Exactly one audit row for this device with the right action tag.
            rows = conn.execute(sa.text("""
                SELECT resource, resource_id, details, "user", action
                  FROM audit_logs
                 WHERE action = 'msp_migration_ambiguous_group_assignment'
                   AND resource_id = 'lib_switch_1'
            """)).fetchall()
            assert len(rows) == 1
            row = rows[0]
            assert row[0] == "device"
            assert row[3] == "msp_migration"

            details_raw = row[2]
            details = (
                json.loads(details_raw)
                if isinstance(details_raw, str)
                else details_raw
            )
            assert isinstance(details, dict)
            assert set(details.get("previous_groups", [])) == {
                circulation_gid,
                reference_gid,
            }
            assert details.get("assigned_to") == default_gid
    finally:
        engine.dispose()
