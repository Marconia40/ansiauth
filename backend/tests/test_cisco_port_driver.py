"""Integration tests for the Cisco port-read driver (Step 1.3).

The parser itself is unit-tested in ``test_cisco_port_parser.py``. This
file covers the driver's playbook-contract plumbing: mapping
``ansible_service`` stdouts to the parser, error handling on playbook
failure, and parser-exception wrapping.

Modernized against the current architecture:
* ``app.services.vendors.cisco.port_driver.CiscoPortDriver`` is deleted
  -- the equivalent read logic now lives on
  ``app.services.vendors.cisco.driver.CiscoVendor.list_ports()``, which
  goes through the shared ``VendorDriver._leer()`` helper (not a direct
  ``ansible_service.run_playbook()`` call) and a single shared playbook,
  ``"vendors/cisco/run.yml"`` (not the old per-operation
  ``"vendors/cisco/get_ports.yml"``).
* ``list_ports()`` now issues 5 commands, not 3 (status, description,
  switchport, storm-control, running-config -- see
  ``app/services/vendors/cisco/commands.yaml``'s ``list_ports`` entry),
  and calls it with ``partial_ok=True`` (adds
  ``"tolerate_command_errors": True`` to the extravars) so a device that
  doesn't support one command (e.g. no ``storm-control``) doesn't tumble
  the whole read.
* ``CiscoPortParser.parse_ports()`` now takes 5 positional string args
  (status, description, switchport, storm, running_config) -- not the
  unrelated 3-arg free function ``parse_ios_ports`` (already tested
  elsewhere).
* ``app.models.port.PortInfo`` is deleted -- ``Puerto`` is the single
  read+write port model now.
* The old ``get_port()``-specific tests have NO successor: a single-item
  fetch method doesn't exist on ``CiscoVendor``/``VendorDriver`` any more
  (confirmed by reading ``app/services/vendors/base.py`` -- only
  ``get_vlan()`` exists for VLANs, no port equivalent). Dropped, not
  ported.
"""

import pytest

from app.models.device import Device
from app.models.port import Puerto
from app.services import ansible_service
from app.services.parsers.port_parser import CiscoPortParser
from app.services.vendors.cisco.driver import CiscoVendor


def _make_device() -> Device:
    dev = Device(
        name="cisco1", host="192.0.2.10", vendor="cisco_ios",
        username="admin", encrypted_password="encrypted", platform="ios",
    )
    dev._driver = CiscoVendor()
    dev._password = "pw"
    return dev


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


def _run_ports(device):
    return device.driver.list_ports(device, device.password)


# -- Happy path ----------------------------------------------------------------

def test_list_ports_parses_five_command_response(monkeypatch):
    captured: dict = {}

    def _fake_run(playbook, extravars, inventory=None, device=None):
        captured["playbook"] = playbook
        captured["extravars"] = extravars
        return {
            "rc": 0, "stdout": "", "stderr": "",
            "stdouts": [_STATUS, _DESC, _SW, "", ""],
        }

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run)
    dev = _make_device()
    ports = _run_ports(dev)

    assert captured["playbook"] == "vendors/cisco/run.yml"
    assert captured["extravars"]["device"] == "cisco1"
    assert captured["extravars"]["tolerate_command_errors"] is True
    assert len(captured["extravars"]["commands"]) == 5

    assert len(ports) == 1
    p = ports[0]
    assert isinstance(p, Puerto)
    assert p.interface == "Gi0/1"
    assert p.description == "web-server-01"
    assert p.admin_up is True
    assert p.operational_up is True
    assert p.mode == "access"
    assert p.access_vlan == 10


def test_list_ports_handles_partial_stdouts(monkeypatch):
    """Only the first command's output came back -- no switchport data
    means no L2 ports are surfaced (L3/no-switchport filtering is
    intentional -- see CiscoPortParser's source-of-truth strategy)."""
    monkeypatch.setattr(
        ansible_service, "run_playbook",
        lambda **kw: {"rc": 0, "stdout": "", "stderr": "", "stdouts": [_STATUS]},
    )
    ports = _run_ports(_make_device())
    assert ports == []


# -- Error paths ----------------------------------------------------------------

def test_list_ports_raises_runtime_error_on_non_zero_rc(monkeypatch):
    monkeypatch.setattr(
        ansible_service, "run_playbook",
        lambda **kw: {"rc": 1, "stdout": "", "stderr": "SSH timeout", "stdouts": []},
    )
    with pytest.raises(RuntimeError) as exc:
        _run_ports(_make_device())
    assert "SSH timeout" in str(exc.value)
    assert "cisco1" in str(exc.value)


def test_list_ports_raises_when_stdouts_missing(monkeypatch):
    """A successful playbook with no captured command outputs is a bug in
    the playbook itself -- surface it as a clear RuntimeError."""
    monkeypatch.setattr(
        ansible_service, "run_playbook",
        lambda **kw: {"rc": 0, "stdout": "", "stderr": "", "stdouts": []},
    )
    with pytest.raises(RuntimeError) as exc:
        _run_ports(_make_device())
    assert "no command output" in str(exc.value).lower()


def test_list_ports_wraps_parser_errors(monkeypatch):
    """If the parser raises, the driver re-raises as RuntimeError with the
    device name in the message."""
    monkeypatch.setattr(
        ansible_service, "run_playbook",
        lambda **kw: {"rc": 0, "stdout": "", "stderr": "", "stdouts": [_STATUS, _DESC, _SW, "", ""]},
    )

    def _boom(*args, **kwargs):
        raise ValueError("simulated parser failure")

    monkeypatch.setattr(CiscoPortParser, "parse_ports", classmethod(lambda cls, *a, **k: _boom()))

    with pytest.raises(RuntimeError) as exc:
        _run_ports(_make_device())
    assert "simulated parser failure" in str(exc.value)
    assert "cisco1" in str(exc.value)
