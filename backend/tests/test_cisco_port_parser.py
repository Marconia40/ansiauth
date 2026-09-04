"""Unit tests for the Cisco IOS port parser (Step 1.3).

These pin the contract that ``parse_ios_ports`` and its per-command helpers
must satisfy: physical-port filtering, admin/oper state mapping across
``connected``/``notconnect``/``disabled``/``err-disabled``, description
handling with empty / wrapped descriptions, trunk VLAN list expansion
across the formats IOS emits (ALL / NONE / comma-list / ranges / wrapped
continuations), and the policy that L3 / non-switchport interfaces are
excluded.
"""

from app.services.parsers.port_parser import (
    parse_ios_interface_description,
    parse_ios_interface_status,
    parse_ios_ports,
    parse_ios_switchport,
)


# ── Fixtures: realistic IOS output samples ───────────────────────────────────

STATUS_SAMPLE = """\
Port      Name               Status       Vlan       Duplex  Speed Type
Gi0/1     Workstation01      connected    10         a-full  a-1000 10/100/1000BaseTX
Gi0/2                        notconnect   20         auto    auto   10/100/1000BaseTX
Gi0/3     Trunk to core      connected    trunk      full    1000   1000BaseTX SFP
Gi0/4                        disabled     1          auto    auto   10/100/1000BaseTX
Gi0/5     uplink             err-disabled 1          auto    auto   10/100/1000BaseTX
Po1                          connected    trunk      a-full  a-1000
Vl10                         connected    routed     auto    auto
"""

DESCRIPTION_SAMPLE = """\
Interface                      Status         Protocol Description
Gi0/1                          up             up       Workstation-01-detailed
Gi0/2                          down           down     Reserved port for printer
Gi0/3                          up             up       Trunk to core switch
Gi0/4                          admin down     down     Shut by ops
Gi0/5                          up (disabled)  down     err-disabled by STP
Gi0/6                          up             up       multi word description here
Gi0/7                          up             up
Po1                            up             up       L2 LACP bundle to dist1
Vl10                           up             up       L3 SVI for VLAN 10
"""

SWITCHPORT_SAMPLE = """\
Name: Gi0/1
Switchport: Enabled
Administrative Mode: static access
Operational Mode: static access
Administrative Trunking Encapsulation: dot1q
Operational Trunking Encapsulation: native
Negotiation of Trunking: Off
Access Mode VLAN: 10 (HQ_VLAN_10)
Trunking Native Mode VLAN: 1 (default)
Voice VLAN: none
Trunking VLANs Enabled: ALL
Pruning VLANs Enabled: 2-1001

Name: Gi0/2
Switchport: Enabled
Administrative Mode: static access
Operational Mode: static access
Access Mode VLAN: 20 (PRINTERS)
Trunking Native Mode VLAN: 1 (default)
Trunking VLANs Enabled: ALL

Name: Gi0/3
Switchport: Enabled
Administrative Mode: trunk
Operational Mode: trunk
Administrative Trunking Encapsulation: dot1q
Operational Trunking Encapsulation: dot1q
Negotiation of Trunking: On
Access Mode VLAN: 1 (default)
Trunking Native Mode VLAN: 99 (NATIVE_VLAN)
Administrative Native VLAN tagging: enabled
Voice VLAN: none
Trunking VLANs Enabled: 1,3-10,20,30,
40,50,60-100
Pruning VLANs Enabled: 2-1001

Name: Gi0/4
Switchport: Enabled
Administrative Mode: static access
Operational Mode: static access
Access Mode VLAN: 1 (default)
Trunking Native Mode VLAN: 1 (default)
Trunking VLANs Enabled: ALL

Name: Gi0/5
Switchport: Enabled
Administrative Mode: static access
Operational Mode: static access
Access Mode VLAN: 30 (DMZ)
Trunking Native Mode VLAN: 1 (default)
Trunking VLANs Enabled: ALL

Name: Gi0/6
Switchport: Enabled
Administrative Mode: dynamic desirable
Operational Mode: static access
Access Mode VLAN: 40 (USERS)
Trunking Native Mode VLAN: 1 (default)
Trunking VLANs Enabled: ALL

Name: Gi0/7
Switchport: Enabled
Administrative Mode: trunk
Operational Mode: down
Access Mode VLAN: 1 (default)
Trunking Native Mode VLAN: 1 (default)
Trunking VLANs Enabled: NONE

Name: Gi0/99
Switchport: Disabled
Administrative Mode: static access
Operational Mode: down

Name: Po1
Switchport: Enabled
Administrative Mode: trunk
Operational Mode: trunk
Access Mode VLAN: 1 (default)
Trunking Native Mode VLAN: 1 (default)
Trunking VLANs Enabled: 100-200

Name: Vl10
Switchport: Disabled
Administrative Mode: static access
"""


# ── show interfaces status ───────────────────────────────────────────────────

def test_status_filters_non_physical_interfaces():
    rows = parse_ios_interface_status(STATUS_SAMPLE)
    # Vl10 is an SVI and must not be in the result.
    assert "Vl10" not in rows
    assert set(rows.keys()) == {"Gi0/1", "Gi0/2", "Gi0/3", "Gi0/4", "Gi0/5", "Po1"}


def test_status_admin_oper_mapping_connected():
    rows = parse_ios_interface_status(STATUS_SAMPLE)
    assert rows["Gi0/1"].admin_up is True
    assert rows["Gi0/1"].operational_up is True


def test_status_notconnect_means_admin_up_oper_down():
    rows = parse_ios_interface_status(STATUS_SAMPLE)
    assert rows["Gi0/2"].admin_up is True
    assert rows["Gi0/2"].operational_up is False


def test_status_disabled_means_admin_down():
    rows = parse_ios_interface_status(STATUS_SAMPLE)
    assert rows["Gi0/4"].admin_up is False
    assert rows["Gi0/4"].operational_up is False


def test_status_err_disabled_admin_up_oper_down():
    rows = parse_ios_interface_status(STATUS_SAMPLE)
    # err-disabled = the operator did not shut the port, but it isn't passing
    # traffic — so admin_up is True, operational_up is False.
    assert rows["Gi0/5"].admin_up is True
    assert rows["Gi0/5"].operational_up is False


def test_status_empty_input():
    assert parse_ios_interface_status("") == {}


# ── show interfaces description ──────────────────────────────────────────────

def test_description_extracts_full_string_with_spaces():
    rows = parse_ios_interface_description(DESCRIPTION_SAMPLE)
    assert rows["Gi0/6"].description == "multi word description here"


def test_description_admin_down_mapping():
    rows = parse_ios_interface_description(DESCRIPTION_SAMPLE)
    assert rows["Gi0/4"].admin_up is False
    assert rows["Gi0/4"].operational_up is False


def test_description_up_disabled_means_admin_up_oper_down():
    rows = parse_ios_interface_description(DESCRIPTION_SAMPLE)
    # "up (disabled)" is err-disabled — operator did not admin-down.
    assert rows["Gi0/5"].admin_up is True
    assert rows["Gi0/5"].operational_up is False


def test_description_down_protocol_down_admin_up():
    rows = parse_ios_interface_description(DESCRIPTION_SAMPLE)
    # "down" status = link physically down but admin did not shutdown.
    assert rows["Gi0/2"].admin_up is True
    assert rows["Gi0/2"].operational_up is False


def test_description_empty_description_becomes_none():
    rows = parse_ios_interface_description(DESCRIPTION_SAMPLE)
    assert rows["Gi0/7"].description is None


def test_description_excludes_svi():
    rows = parse_ios_interface_description(DESCRIPTION_SAMPLE)
    assert "Vl10" not in rows


# ── show interfaces switchport ───────────────────────────────────────────────

def test_switchport_access_mode():
    rows = parse_ios_switchport(SWITCHPORT_SAMPLE)
    row = rows["Gi0/1"]
    assert row.mode == "access"
    assert row.access_vlan == 10
    # access ports get allowed_vlans=None even when IOS still echoes "ALL"
    assert row.allowed_vlans is None


def test_switchport_trunk_with_wrapped_vlan_list():
    rows = parse_ios_switchport(SWITCHPORT_SAMPLE)
    row = rows["Gi0/3"]
    assert row.mode == "trunk"
    # native VLAN populates access_vlan
    assert row.access_vlan == 99
    # The wrapped continuation must be merged correctly.
    expected = sorted({1, 3, 4, 5, 6, 7, 8, 9, 10, 20, 30, 40, 50,
                       60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70,
                       71, 72, 73, 74, 75, 76, 77, 78, 79, 80,
                       81, 82, 83, 84, 85, 86, 87, 88, 89, 90,
                       91, 92, 93, 94, 95, 96, 97, 98, 99, 100})
    assert row.allowed_vlans == expected


def test_switchport_dynamic_mode_falls_back_to_admin():
    """Operational mode 'static access' on a dynamically-negotiated port.

    The parser prefers the operational mode (what the port is actually
    doing), so Gi0/6 ends up reported as access despite admin = dynamic
    desirable.  Admin intent is intentionally ignored when operational is
    a concrete mode.
    """
    rows = parse_ios_switchport(SWITCHPORT_SAMPLE)
    assert rows["Gi0/6"].mode == "access"
    assert rows["Gi0/6"].access_vlan == 40


def test_switchport_operational_down_falls_back_to_admin_trunk():
    """When operational mode is 'down', fall back to administrative intent."""
    rows = parse_ios_switchport(SWITCHPORT_SAMPLE)
    row = rows["Gi0/7"]
    assert row.mode == "trunk"
    # NONE means an empty list, not None.
    assert row.allowed_vlans == []


def test_switchport_filters_switchport_disabled():
    rows = parse_ios_switchport(SWITCHPORT_SAMPLE)
    # Gi0/99 has Switchport: Disabled — it's an L3 routed port and must be excluded.
    assert "Gi0/99" not in rows


def test_switchport_filters_svi():
    rows = parse_ios_switchport(SWITCHPORT_SAMPLE)
    assert "Vl10" not in rows


def test_switchport_port_channel_included():
    rows = parse_ios_switchport(SWITCHPORT_SAMPLE)
    assert "Po1" in rows
    assert rows["Po1"].mode == "trunk"
    assert rows["Po1"].allowed_vlans == list(range(100, 201))


def test_switchport_trunk_all_expands_to_full_range():
    rows = parse_ios_switchport(SWITCHPORT_SAMPLE)
    # Trunk ALL = every VLAN.
    # (Use a port that's actually trunk — Gi0/3 has a list, Po1 has 100-200)
    # We synthesize a one-off block for this assertion.
    extra = """\
Name: Te1/1
Switchport: Enabled
Administrative Mode: trunk
Operational Mode: trunk
Access Mode VLAN: 1 (default)
Trunking Native Mode VLAN: 1 (default)
Trunking VLANs Enabled: ALL
"""
    extra_rows = parse_ios_switchport(extra)
    assert extra_rows["Te1/1"].allowed_vlans == list(range(1, 4095))


def test_switchport_empty_input():
    assert parse_ios_switchport("") == {}


# ── Combined parser ──────────────────────────────────────────────────────────

def test_combined_parser_merges_three_sources():
    ports = parse_ios_ports(STATUS_SAMPLE, DESCRIPTION_SAMPLE, SWITCHPORT_SAMPLE)
    by_name = {p.interface: p for p in ports}

    # The L3 SVI must not surface as a port.
    assert "Vl10" not in by_name
    # Switchport-disabled port must not surface.
    assert "Gi0/99" not in by_name

    # Output sorted by name
    assert [p.interface for p in ports] == sorted(p.interface for p in ports)

    g1 = by_name["Gi0/1"]
    assert g1.description == "Workstation-01-detailed"
    assert g1.admin_up is True
    assert g1.operational_up is True
    assert g1.mode == "access"
    assert g1.access_vlan == 10
    assert g1.allowed_vlans is None

    g4 = by_name["Gi0/4"]
    assert g4.description == "Shut by ops"
    assert g4.admin_up is False
    assert g4.operational_up is False

    # err-disabled
    g5 = by_name["Gi0/5"]
    assert g5.admin_up is True
    assert g5.operational_up is False

    # Port-channel L2 bundle is included
    assert "Po1" in by_name

    # Step 1.3 explicitly leaves PoE / speed / duplex as None
    for p in ports:
        assert p.poe_enabled is None
        assert p.speed is None
        assert p.duplex is None


def test_combined_parser_tolerates_missing_description_source():
    # If description output is missing entirely, status_output supplies
    # admin / oper state.  description field stays None.
    ports = parse_ios_ports(STATUS_SAMPLE, "", SWITCHPORT_SAMPLE)
    by_name = {p.interface: p for p in ports}
    g1 = by_name["Gi0/1"]
    assert g1.description is None
    assert g1.admin_up is True
    assert g1.operational_up is True
    assert g1.mode == "access"
    assert g1.access_vlan == 10


def test_combined_parser_tolerates_missing_status_source():
    # If status output is missing, we still get full data from description
    # + switchport.
    ports = parse_ios_ports("", DESCRIPTION_SAMPLE, SWITCHPORT_SAMPLE)
    by_name = {p.interface: p for p in ports}
    assert by_name["Gi0/3"].mode == "trunk"
    assert by_name["Gi0/3"].admin_up is True
    assert by_name["Gi0/3"].operational_up is True


def test_combined_parser_tolerates_empty_everywhere():
    assert parse_ios_ports("", "", "") == []


def test_combined_parser_handles_ports_only_in_switchport():
    """A port present in switchport but missing from both other commands.

    Should still appear in the output with admin/oper/description=None.
    """
    sw_only = """\
Name: Gi0/42
Switchport: Enabled
Administrative Mode: static access
Operational Mode: static access
Access Mode VLAN: 50 (TEST)
Trunking Native Mode VLAN: 1 (default)
Trunking VLANs Enabled: ALL
"""
    ports = parse_ios_ports("", "", sw_only)
    assert len(ports) == 1
    p = ports[0]
    assert p.interface == "Gi0/42"
    assert p.admin_up is None
    assert p.operational_up is None
    assert p.description is None
    assert p.mode == "access"
    assert p.access_vlan == 50


def test_combined_parser_excludes_l3_only_ports():
    """A port in description but absent from switchport is L3-only and excluded."""
    desc_only = """\
Interface                      Status         Protocol Description
Gi0/100                        up             up       Routed uplink
"""
    ports = parse_ios_ports("", desc_only, "")
    assert ports == []
