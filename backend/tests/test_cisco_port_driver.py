"""Integration tests for the Cisco port driver (Step 1.3).

The parser is unit-tested in ``test_cisco_port_parser``.  Here we cover the
driver's playbook-contract contract: mapping ansible_service stdouts to the
parser, error handling on playbook failure, and consistency with the
shared BasePortDriver interface.
"""

import pytest

from app.models.device import Device
from app.models.port import PortInfo
from app.services import ansible_service
from app.services.vendors.cisco.port_driver import CiscoPortDriver


def _make_device() -> Device:
    return Device(
        name="cisco1",
        host="192.0.2.10",
        vendor="cisco_ios",
        username="admin",
        encrypted_password="encrypted",
        platform="ios",
    )


# Reusable realistic outputs ─ borrowed shape from the parser tests but kept
# minimal to keep these driver tests focused on the playbook plumbing.
_STATUS = """\
Port      Name               Status       Vlan       Duplex  Speed Type
Gi0/1     web01              connected    10         a-full  a-1000 10/100/1000BaseTX
"""

_DESC = """\
Interface                      Status         Protocol Description
Gi0/1                          up             up       web-server-01
"""

_SW = """\
Name: Gi0/1
Switchport: Enabled
Administrative Mode: static access
Operational Mode: static access
Access Mode VLAN: 10 (WEB)
Trunking Native Mode VLAN: 1 (default)
Trunking VLANs Enabled: ALL
"""


# ── Happy path ────────────────────────────────────────────────────────────────

def test_list_ports_parses_three_command_response(monkeypatch):
    captured: dict = {}

    def _fake_run(playbook, extravars, inventory=None, device=None):
        captured["playbook"] = playbook
        captured["extravars"] = extravars
        return {
            "rc": 0,
            "stdout": "",
            "stderr": "",
            "stdouts": [_STATUS, _DESC, _SW],
        }

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run)
    driver = CiscoPortDriver()
    ports = driver.list_ports(_make_device(), "pw")

    assert captured["playbook"] == "vendors/cisco/get_ports.yml"
    assert captured["extravars"] == {"device": "cisco1"}
    assert len(ports) == 1
    p = ports[0]
    assert isinstance(p, PortInfo)
    assert p.name == "Gi0/1"
    assert p.description == "web-server-01"
    assert p.admin_up is True
    assert p.operational_up is True
    assert p.mode == "access"
    assert p.access_vlan == 10


def test_list_ports_handles_partial_stdouts(monkeypatch):
    """If the playbook returned only the first command's output we should
    still get back the ports it can derive from switchport=`` and degrade
    other fields gracefully — but in this case switchport is empty so the
    output is an empty port list (no L2 switchports detected)."""
    monkeypatch.setattr(
        ansible_service,
        "run_playbook",
        lambda **kw: {"rc": 0, "stdout": "", "stderr": "", "stdouts": [_STATUS]},
    )
    ports = CiscoPortDriver().list_ports(_make_device(), "pw")
    # No switchport data → no L2 ports surfaced (L3 filtering is intentional).
    assert ports == []


# ── Error paths ──────────────────────────────────────────────────────────────

def test_list_ports_raises_runtime_error_on_non_zero_rc(monkeypatch):
    monkeypatch.setattr(
        ansible_service,
        "run_playbook",
        lambda **kw: {"rc": 1, "stdout": "", "stderr": "SSH timeout", "stdouts": []},
    )
    with pytest.raises(RuntimeError) as exc:
        CiscoPortDriver().list_ports(_make_device(), "pw")
    assert "SSH timeout" in str(exc.value)
    assert "cisco1" in str(exc.value)


def test_list_ports_raises_when_stdouts_missing(monkeypatch):
    """A successful playbook with no captured command outputs is a bug
    in the playbook itself — surface it as a clear RuntimeError."""
    monkeypatch.setattr(
        ansible_service,
        "run_playbook",
        lambda **kw: {"rc": 0, "stdout": "", "stderr": "", "stdouts": []},
    )
    with pytest.raises(RuntimeError) as exc:
        CiscoPortDriver().list_ports(_make_device(), "pw")
    assert "no command outputs" in str(exc.value).lower()


def test_list_ports_wraps_parser_errors(monkeypatch):
    """If the parser raises (regex blow-up / unexpected None deref), the
    driver re-raises as RuntimeError with the device name in the message."""
    monkeypatch.setattr(
        ansible_service,
        "run_playbook",
        lambda **kw: {"rc": 0, "stdout": "", "stderr": "", "stdouts": [_STATUS, _DESC, _SW]},
    )

    from app.services.vendors.cisco import port_driver as cisco_port_driver

    def _boom(*args, **kwargs):
        raise ValueError("simulated parser failure")

    monkeypatch.setattr(cisco_port_driver, "parse_ios_ports", _boom)

    with pytest.raises(RuntimeError) as exc:
        CiscoPortDriver().list_ports(_make_device(), "pw")
    assert "simulated parser failure" in str(exc.value)
    assert "cisco1" in str(exc.value)


# ── get_port default behaviour inherited from BasePortDriver ─────────────────

def test_get_port_returns_matching_port(monkeypatch):
    monkeypatch.setattr(
        ansible_service,
        "run_playbook",
        lambda **kw: {"rc": 0, "stdout": "", "stderr": "", "stdouts": [_STATUS, _DESC, _SW]},
    )
    driver = CiscoPortDriver()
    p = driver.get_port("Gi0/1", _make_device(), "pw")
    assert p is not None
    assert p.name == "Gi0/1"


def test_get_port_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(
        ansible_service,
        "run_playbook",
        lambda **kw: {"rc": 0, "stdout": "", "stderr": "", "stdouts": [_STATUS, _DESC, _SW]},
    )
    assert CiscoPortDriver().get_port("Gi9/9/9", _make_device(), "pw") is None
