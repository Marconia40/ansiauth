"""Unit tests for the Huawei VRP port parser.

These tests pin the contract that ``parse_vrp_ports`` and its per-command
helpers must satisfy: physical-port filtering, admin/oper state mapping,
description handling (including empty descriptions and ones with spaces),
and trunk VLAN list expansion across the formats VRP emits in the wild.
"""

from app.services.parsers.port_parser import (
    parse_vrp_interface_brief,
    parse_vrp_interface_description,
    parse_vrp_port_vlan,
    parse_vrp_ports,
)


# ── display interface brief ──────────────────────────────────────────────────

BRIEF_SAMPLE = """\
PHY: Physical
*down: administratively down
^down: standby
(l): loopback
(s): spoofing
(b): BFD down
(e): ETHOAM down
(d): Dampening Suppressed
(p): port alarm down
(dl): DLDP down
(lb): LBDT down
(ld): link-flap down
(ng): NGE error down
InUti/OutUti: input utility/output utility
Interface                   PHY     Protocol  InUti OutUti   inErrors  outErrors
GigabitEthernet0/0/1        up      up        0.01%  0.01%        0        0
GigabitEthernet0/0/2        *down   down      0%     0%           0        0
GigabitEthernet0/0/3        down    down      0%     0%           0        0
GigabitEthernet0/0/4        up      up(s)     0%     0%           0        0
Vlanif1                     up      up        --     --           0        0
NULL0                       up      up(s)     --     --           0        0
"""


def test_brief_filters_pseudo_interfaces():
    rows = parse_vrp_interface_brief(BRIEF_SAMPLE)
    # Vlanif1 and NULL0 should not be in the result
    assert "Vlanif1" not in rows
    assert "NULL0" not in rows
    assert set(rows.keys()) == {
        "GigabitEthernet0/0/1",
        "GigabitEthernet0/0/2",
        "GigabitEthernet0/0/3",
        "GigabitEthernet0/0/4",
    }


def test_brief_admin_state_mapping():
    rows = parse_vrp_interface_brief(BRIEF_SAMPLE)
    assert rows["GigabitEthernet0/0/1"].admin_up is True
    # *down means administratively down
    assert rows["GigabitEthernet0/0/2"].admin_up is False
    # Plain "down" still means admin-up (operator did not shut it)
    assert rows["GigabitEthernet0/0/3"].admin_up is True
    assert rows["GigabitEthernet0/0/4"].admin_up is True


def test_brief_operational_state_mapping():
    rows = parse_vrp_interface_brief(BRIEF_SAMPLE)
    assert rows["GigabitEthernet0/0/1"].operational_up is True
    assert rows["GigabitEthernet0/0/2"].operational_up is False
    assert rows["GigabitEthernet0/0/3"].operational_up is False
    # up(s) — spoofing variant — should still count as operational
    assert rows["GigabitEthernet0/0/4"].operational_up is True


def test_brief_empty_input():
    assert parse_vrp_interface_brief("") == {}


# ── display interface description ────────────────────────────────────────────

DESCRIPTION_SAMPLE = """\
PHY: Physical
*down: administratively down
^down: standby
(l): loopback
(s): spoofing
Interface                  PHY      Protocol Description
GigabitEthernet0/0/1       up       up       Workstation01
GigabitEthernet0/0/2       *down    down     Reserved port for printer
GigabitEthernet0/0/3       up       up
GigabitEthernet0/0/4       up       up       multi word description here
"""


def test_descriptions_extracted_with_spaces():
    rows = parse_vrp_interface_description(DESCRIPTION_SAMPLE)
    assert rows["GigabitEthernet0/0/1"] == "Workstation01"
    assert rows["GigabitEthernet0/0/2"] == "Reserved port for printer"
    assert rows["GigabitEthernet0/0/4"] == "multi word description here"


def test_empty_description_becomes_none():
    rows = parse_vrp_interface_description(DESCRIPTION_SAMPLE)
    assert rows["GigabitEthernet0/0/3"] is None


# ── display port vlan ────────────────────────────────────────────────────────

PORT_VLAN_SAMPLE = """\
Port                    Link Type    PVID  Trunk VLAN List
-------------------------------------------------------------------
GigabitEthernet0/0/1    access       10    -
GigabitEthernet0/0/2    trunk        1     2 to 4094
GigabitEthernet0/0/3    hybrid       1     1 untagged, 100 tagged
GigabitEthernet0/0/4    trunk        99    10, 20, 30 to 32
GigabitEthernet0/0/5    access       1     -
"""


def test_port_vlan_access_mode():
    rows = parse_vrp_port_vlan(PORT_VLAN_SAMPLE)
    row = rows["GigabitEthernet0/0/1"]
    assert row.mode == "access"
    assert row.access_vlan == 10
    assert row.allowed_vlans is None


def test_port_vlan_trunk_with_range():
    rows = parse_vrp_port_vlan(PORT_VLAN_SAMPLE)
    row = rows["GigabitEthernet0/0/2"]
    assert row.mode == "trunk"
    assert row.access_vlan == 1
    # Full range expanded
    assert row.allowed_vlans is not None
    assert row.allowed_vlans[0] == 2
    assert row.allowed_vlans[-1] == 4094
    assert len(row.allowed_vlans) == 4093


def test_port_vlan_hybrid_normalized_to_unknown():
    rows = parse_vrp_port_vlan(PORT_VLAN_SAMPLE)
    row = rows["GigabitEthernet0/0/3"]
    # Hybrid mode is not access/trunk so should be normalized to unknown
    assert row.mode == "unknown"
    # PVID still captured
    assert row.access_vlan == 1
    # Allowed VLANs parsed best-effort from "1 untagged, 100 tagged"
    assert row.allowed_vlans == [1, 100]


def test_port_vlan_trunk_mixed_list_and_range():
    rows = parse_vrp_port_vlan(PORT_VLAN_SAMPLE)
    row = rows["GigabitEthernet0/0/4"]
    assert row.mode == "trunk"
    assert row.access_vlan == 99
    assert row.allowed_vlans == [10, 20, 30, 31, 32]


def test_port_vlan_empty_input():
    assert parse_vrp_port_vlan("") == {}


# ── End-to-end combined parser ───────────────────────────────────────────────

def test_combined_parser_merges_three_sources():
    ports = parse_vrp_ports(BRIEF_SAMPLE, DESCRIPTION_SAMPLE, PORT_VLAN_SAMPLE)
    by_name = {p.name: p for p in ports}

    # Pseudo-interfaces excluded across all three sources
    assert "Vlanif1" not in by_name
    assert "NULL0" not in by_name

    # Output sorted by name
    assert [p.name for p in ports] == sorted(p.name for p in ports)

    g1 = by_name["GigabitEthernet0/0/1"]
    assert g1.description == "Workstation01"
    assert g1.admin_up is True
    assert g1.operational_up is True
    assert g1.mode == "access"
    assert g1.access_vlan == 10
    assert g1.allowed_vlans is None

    g2 = by_name["GigabitEthernet0/0/2"]
    assert g2.description == "Reserved port for printer"
    assert g2.admin_up is False
    assert g2.operational_up is False

    # Step 1.1 leaves PoE / speed / duplex as None (do not invent values)
    for p in ports:
        assert p.poe_enabled is None
        assert p.speed is None
        assert p.duplex is None


def test_combined_parser_tolerates_missing_sources():
    # No brief input: admin/oper become None, but mode/VLANs still come through
    ports = parse_vrp_ports("", "", PORT_VLAN_SAMPLE)
    by_name = {p.name: p for p in ports}
    g1 = by_name["GigabitEthernet0/0/1"]
    assert g1.admin_up is None
    assert g1.operational_up is None
    assert g1.description is None
    assert g1.mode == "access"
    assert g1.access_vlan == 10


def test_combined_parser_tolerates_empty_everywhere():
    assert parse_vrp_ports("", "", "") == []


def test_combined_parser_handles_ports_only_in_one_source():
    # Port only listed in the brief — its mode falls back to "unknown".
    brief_only = """\
Interface                   PHY     Protocol  InUti OutUti   inErrors  outErrors
GigabitEthernet0/0/99       up      up        0%     0%           0        0
"""
    ports = parse_vrp_ports(brief_only, "", "")
    assert len(ports) == 1
    assert ports[0].name == "GigabitEthernet0/0/99"
    assert ports[0].mode == "unknown"
    assert ports[0].admin_up is True
    assert ports[0].operational_up is True
