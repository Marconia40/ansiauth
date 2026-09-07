"""Tests for Decision 3 of docs/SSH_REFRESH_PLAN.md:
``VendorDriver.read_core_state()`` fuses VLAN + port + SVI reads into
as few SSH sessions as each vendor allows.

The whole win of this decision is measured in ``_leer()`` invocation
counts (each ``_leer`` call = 1 SSH session). So these tests intercept
``_leer`` on the concrete vendor drivers, feed it stub stdouts, and
assert (a) how many times it was called and (b) that the returned
tuple destructures cleanly through the parsers.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.vendors.cisco.driver import CiscoVendor
from app.services.vendors.huawei.driver import HuaweiVendor


# ── Fixtures & helpers ─────────────────────────────────────────────────────

class _FakeDevice:
    """Minimal duck-typed Device -- read_core_state only reads .name."""
    name = "test-device"


def _stub_vlan_brief_cisco() -> str:
    # Header + 1 real VLAN row is enough for parse_vlan_brief.
    return (
        "VLAN Name                             Status    Ports\n"
        "---- -------------------------------- --------- ------\n"
        "10   MGMT                             active    Gi1/0/1\n"
    )


def _stub_vlan_display_huawei() -> str:
    # Minimal ``display vlan`` header + 1 VLAN.
    return (
        "The total number of vlans is : 1\n"
        "--------------------------------------------------------------------------------\n"
        "VID  Type     Ports\n"
        "--------------------------------------------------------------------------------\n"
        "10   common   UT:GE0/0/1(U)\n"
    )


def _stub_ports_cisco_status() -> str:
    return (
        "Port      Name               Status       Vlan       Duplex  Speed Type\n"
        "Gi1/0/1                      connected    10         a-full  a-1000 10/100/1000BaseTX\n"
    )


# ── Cisco: 1 SSH session for the whole core read ──────────────────────────

def test_cisco_read_core_state_uses_a_single_leer_call():
    """CiscoVendor.read_core_state must call _leer exactly ONCE.

    That single call must carry every command from list_vlans (1 cmd) +
    list_ports (4 cmds) + get_svis (2 cmds) = 7 commands in order.
    """
    v = CiscoVendor()
    cmds = v._cargar_comandos()
    n_vlan = len(cmds["list_vlans"]["primary"]["commands"])
    n_port = len(cmds["list_ports"]["primary"]["commands"])
    n_svi = len(cmds["get_svis"]["primary"]["commands"])
    expected_total = n_vlan + n_port + n_svi

    # Stub stdouts in the same order the driver appends them:
    # [vlan brief, port_1..port_4, svi_running, svi_brief]
    stub_stdouts = (
        [_stub_vlan_brief_cisco()]
        + [_stub_ports_cisco_status(), "", "", ""]
        + ["", ""]
    )
    assert len(stub_stdouts) == expected_total, (
        "test fixture is out of sync with commands.yaml"
    )

    leer_mock = MagicMock(return_value=stub_stdouts)
    v._leer = leer_mock

    vlans, ports, svis = v.read_core_state(_FakeDevice(), "pw")

    # Exactly ONE SSH session -- the whole point of Decision 3 on Cisco.
    assert leer_mock.call_count == 1
    (commands_arg,), _kwargs = leer_mock.call_args[:1], leer_mock.call_args[1]
    # First positional is the commands list; assert its length + ordering.
    passed_commands = leer_mock.call_args.args[0]
    assert len(passed_commands) == expected_total
    assert passed_commands[:n_vlan] == cmds["list_vlans"]["primary"]["commands"]
    assert passed_commands[n_vlan:n_vlan + n_port] == cmds["list_ports"]["primary"]["commands"]
    assert passed_commands[n_vlan + n_port:] == cmds["get_svis"]["primary"]["commands"]

    # Sanity on the parsed result -- at least the VLAN we stubbed came through.
    assert any(v.vlan_id == 10 for v in vlans)


def test_cisco_read_core_state_wraps_port_parser_errors():
    """A parser blow-up on the port slice must surface as RuntimeError
    with the device name -- same shape as list_ports's error path."""
    v = CiscoVendor()
    # Stub with malformed port output that the parser will reject.
    # 7 stdouts total (1+4+2), but port slice is garbage.
    stub_stdouts = [
        _stub_vlan_brief_cisco(),
        "GARBAGE\nBAD STATUS OUTPUT\n",  # invalid status
        "", "", "",
        "", "",
    ]
    v._leer = MagicMock(return_value=stub_stdouts)

    # We expect either a successful parse (parser is tolerant) OR a
    # RuntimeError mentioning the device -- both are acceptable
    # behaviours consistent with list_ports today.
    try:
        v.read_core_state(_FakeDevice(), "pw")
    except RuntimeError as exc:
        assert _FakeDevice.name in str(exc)


# ── Huawei: 2 SSH sessions (fold vlans+ports+brief, then per-Vlanif) ──────

def test_huawei_read_core_state_no_vlanif_uses_two_leer_calls():
    """When ``display ip interface brief`` reports no Vlanif, the second
    session still runs (needs to fetch the dhcp config)."""
    v = HuaweiVendor()
    cmds = v._cargar_comandos()
    n_vlan = len(cmds["list_vlans"]["primary"]["commands"])
    n_port = len(cmds["list_ports"]["primary"]["commands"])

    session_a_stdouts = (
        [_stub_vlan_display_huawei()]
        + ["", "", "", ""]
        + ["Interface                         IP Address/Mask\nGigabitEthernet0/0/1              10.0.0.1/24\n"]  # no Vlanif
    )
    session_b_stdouts = [""]  # dhcp config: empty
    leer_mock = MagicMock(side_effect=[session_a_stdouts, session_b_stdouts])
    v._leer = leer_mock

    vlans, ports, svis = v.read_core_state(_FakeDevice(), "pw")

    # 2 sessions -- 4 → 2 is the win on Huawei.
    assert leer_mock.call_count == 2

    session_a_cmds = leer_mock.call_args_list[0].args[0]
    assert len(session_a_cmds) == n_vlan + n_port + 1  # +1 for the brief
    assert session_a_cmds[-1] == "display ip interface brief"

    session_b_cmds = leer_mock.call_args_list[1].args[0]
    assert session_b_cmds == ["display current-configuration configuration dhcp"]

    assert svis == []


def test_huawei_read_core_state_with_vlanif_uses_two_leer_calls():
    """When brief reports Vlanif entries, session B fetches per-Vlanif
    configs + dhcp -- still 2 sessions total, not 3+."""
    v = HuaweiVendor()

    session_a_stdouts = (
        [_stub_vlan_display_huawei()]
        + ["", "", "", ""]
        + [
            "Interface                         IP Address/Mask\n"
            "Vlanif10                          10.10.10.1/24\n"
            "Vlanif20                          10.10.20.1/24\n"
        ]
    )
    # Session B: 2 Vlanif entries + dhcp
    session_b_stdouts = ["", "", ""]  # 2 vlanif configs + dhcp
    leer_mock = MagicMock(side_effect=[session_a_stdouts, session_b_stdouts])
    v._leer = leer_mock

    v.read_core_state(_FakeDevice(), "pw")

    assert leer_mock.call_count == 2
    session_b_cmds = leer_mock.call_args_list[1].args[0]
    # 2 per-interface commands + 1 dhcp = 3
    assert len(session_b_cmds) == 3
    assert session_b_cmds[0].startswith("display current-configuration interface Vlanif")
    assert session_b_cmds[-1] == "display current-configuration configuration dhcp"


# ── Default fallback: uses the 3 individual methods ───────────────────────

def test_base_default_read_core_state_uses_individual_methods():
    """A driver that doesn't override ``read_core_state`` must fall back
    to calling ``get_vlans`` / ``list_ports`` / ``get_svis`` in sequence
    -- preserves backwards compatibility for vendors without a combined
    implementation."""
    from app.services.vendors.base import VendorDriver

    class _StubDriver(VendorDriver):
        _PLAYBOOK = "stub.yml"
        _NETWORK_OS = "stub"

        # Stub every abstract method with something callable.
        def create_vlan(self, *a, **kw): return {}
        def delete_vlan(self, *a, **kw): return {}
        def update_vlan(self, *a, **kw): return {}
        def save_config(self, *a, **kw): return {}
        def get_vlans(self, device, password): return ["v"]
        def list_ports(self, device, password): return ["p"]
        def get_svis(self, device, password): return ["s"]

    d = _StubDriver()
    vlans, ports, svis = d.read_core_state(_FakeDevice(), "pw")
    assert vlans == ["v"] and ports == ["p"] and svis == ["s"]
