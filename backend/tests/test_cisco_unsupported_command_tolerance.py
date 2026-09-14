"""Un IOSv/GNS3 sin ``storm-control`` rechaza el 4to comando del batch de
``list_ports`` con ``% Invalid input detected``. Antes eso tumbaba todo el
read; ahora ``_leer(partial_ok=True)`` + ``_filter_unsupported`` degradan
el slot afectado a ``""`` y el parser devuelve los puertos con
``storm_control_*=None``.

Este archivo cubre 3 casos que deben seguir andando:

* Happy path degradado -- storm-control rechazado, resto OK, ports vienen
  con storm_control=None.
* Read all-or-nothing preservado -- device inalcanzable (stdouts vacío)
  sigue lanzando ``RuntimeError``, misma señal que antes para diferenciar
  "feature missing" de "device down".
* Fusion path (``read_core_state``) tolera lo mismo sin arrastrar el
  fallo a las lecturas de VLAN/SVI de la misma sesión SSH.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services import ansible_service
from app.services.vendors.cisco.driver import CiscoVendor


class _FakeDevice:
    name = "cisco-01"
    host = "192.0.2.10"
    username = "admin"
    # VendorDriver._leer() now routes through _ejecutar() (see
    # vendors/base.py), which branches on auth_method to pick
    # ansible_service vs ssh_direct_service -- every test here exercises
    # the ansible/password path (mocking ansible_service.run_playbook),
    # so this stub needs the attribute even though it's not otherwise
    # relevant to what these tests cover.
    auth_method = "password"


_STATUS = (
    "Port      Name               Status       Vlan       Duplex  Speed Type\n"
    "Gi0/1                        connected    10         a-full  a-1000 10/100/1000BaseTX\n"
)
_DESC = (
    "Interface                      Status         Protocol Description\n"
    "Gi0/1                          up             up       \n"
)
_SW = (
    "Name: Gi0/1\n"
    "Switchport: Enabled\n"
    "Administrative Mode: static access\n"
    "Operational Mode: static access\n"
    "Access Mode VLAN: 10 (MGMT)\n"
    "Trunking Native Mode VLAN: 1 (default)\n"
    "Trunking VLANs Enabled: ALL\n"
)
_STORM_INVALID = (
    "show storm-control broadcast\r\n"
    "show storm-control broadcast\r\n"
    " ^\r\n"
    "% Invalid input detected at '^' marker.\r\n\r\n"
    "cisco-01#"
)
# Read exitoso (sin marker de "no soportado") pero sin fila para Gi0/1 --
# el device SÍ soporta storm-control (otro puerto aparece en la tabla),
# este puerto en particular nunca lo tuvo configurado.
_STORM_TABLE_WITHOUT_TARGET_PORT = (
    "Interface  Filter State     Trap State     Upper        Lower        Current\n"
    "--------- ---------------  -------------  -----------  -----------  ----------\n"
    "Gi0/2      inactive         inactive        100.00%      100.00%      0.00%\n"
)
# Puerto shutdown -- la tabla operacional real (formato observado en vivo
# contra f3r9s1, sin columna "Trap State") reporta "Link Down" para
# Gi0/1, que parse_ios_storm_control() a propósito deja como enabled=None
# (no se puede saber Forwarding/Blocking sin link).
_STORM_TABLE_LINK_DOWN = (
    "Key: U - Unicast, B - Broadcast, M - Multicast\n"
    "Interface  Filter State   Upper        Lower        Current     Action     Type\n"
    "---------  -------------  -----------  -----------  ----------  ---------  ----\n"
    "Gi0/1      Link Down           10.00%       10.00%       0.00%  Shut-Trap  B   \n"
)
# Running-config real capturado en vivo contra f3r9s1 (Gi1/0/6, shutdown,
# storm-control SÍ configurado) -- sigue teniendo la config real
# independientemente de que el puerto esté caído.
_RUNNING_CONFIG_WITH_STORM = (
    "interface GigabitEthernet0/1\n"
    " switchport access vlan 258\n"
    " switchport mode access\n"
    " shutdown\n"
    " storm-control broadcast level 10.00\n"
    " storm-control multicast level 10.00\n"
    " storm-control action shutdown\n"
    " storm-control action trap\n"
)


def _make_run_result(rc: int, stdouts: list[str]) -> dict:
    """Mimic what ``ansible_service.run_playbook`` returns."""
    return {"rc": rc, "stdout": "", "stderr": "", "stdouts": stdouts}


# ── Case 1: storm-control rejected, rest of the batch OK ──────────────────

def test_list_ports_tolerates_unsupported_storm_control(monkeypatch):
    """rc != 0 (task failed) + 4 stdouts (last one is ``% Invalid input``)
    must parse a port with storm_control_*=None and NOT raise."""

    def _fake_run(playbook, extravars, inventory=None, device=None):
        return _make_run_result(rc=1, stdouts=[_STATUS, _DESC, _SW, _STORM_INVALID])

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run)

    ports = CiscoVendor().list_ports(_FakeDevice(), "pw")

    assert len(ports) == 1
    p = ports[0]
    assert p.interface == "Gi0/1"
    assert p.mode == "access"
    assert p.access_vlan == 10
    assert p.storm_control_enabled is None
    assert p.storm_control_threshold is None


# ── Case 1b: storm-control supported, port just never configured ──────────

def test_list_ports_storm_control_supported_but_not_configured_is_false(monkeypatch):
    """Bug real encontrado en un job contra f3r9s1: cuando el read de
    storm-control funciona (no rechazado como no-soportado) pero el puerto
    no tiene fila en la tabla, el estado real es "deshabilitado" (Cisco no
    lo habilita por default) -- no "desconocido" como storm_control=None
    hacía creer. Ese None causaba que Puerto.resolver_rollback() tratara un
    rollback legítimo como no-op. Distingue de
    test_list_ports_tolerates_unsupported_storm_control (arriba): ahí
    storm_output queda vacío porque _filter_unsupported() lo pisó; acá
    storm_output SÍ tiene contenido real (otro puerto aparece en la tabla),
    solo que Gi0/1 no está en ella."""

    def _fake_run(playbook, extravars, inventory=None, device=None):
        return _make_run_result(
            rc=0, stdouts=[_STATUS, _DESC, _SW, _STORM_TABLE_WITHOUT_TARGET_PORT],
        )

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run)

    ports = CiscoVendor().list_ports(_FakeDevice(), "pw")

    assert len(ports) == 1
    p = ports[0]
    assert p.interface == "Gi0/1"
    assert p.storm_control_enabled is False
    assert p.storm_control_threshold is None


# ── Case 1c: puerto shutdown -- running-config es autoritativo ────────────

def test_list_ports_storm_control_shutdown_port_uses_running_config(monkeypatch):
    """Bug real reportado por el usuario contra f3r9s1: un puerto shutdown
    con storm-control configurado (threshold real) mostraba
    storm_control_enabled=None en la UI ("—") porque la tabla operacional
    reporta "Link Down" en vez de Forwarding/Blocking cuando no hay link.
    El running-config (ya se lee para action/trap) tiene la config real
    independientemente del link -- debe usarse como fallback."""

    def _fake_run(playbook, extravars, inventory=None, device=None):
        return _make_run_result(
            rc=0,
            stdouts=[_STATUS, _DESC, _SW, _STORM_TABLE_LINK_DOWN, _RUNNING_CONFIG_WITH_STORM],
        )

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run)

    ports = CiscoVendor().list_ports(_FakeDevice(), "pw")

    assert len(ports) == 1
    p = ports[0]
    assert p.interface == "Gi0/1"
    assert p.storm_control_enabled is True
    assert p.storm_control_threshold == 10.0
    assert p.storm_control_action == "shutdown"
    assert p.storm_control_trap is True


# ── Case 2: device truly unreachable -- must still raise ──────────────────

def test_list_ports_still_raises_when_device_unreachable(monkeypatch):
    """rc != 0 AND empty stdouts (transport failure -- SSH auth, ping,
    unreachable) must keep raising RuntimeError. Otherwise we'd silently
    return zero ports on a broken device, which is worse than the noisy
    failure it replaces."""

    def _fake_run(playbook, extravars, inventory=None, device=None):
        return _make_run_result(rc=1, stdouts=[])

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run)

    with pytest.raises(RuntimeError, match="cisco-01"):
        CiscoVendor().list_ports(_FakeDevice(), "pw")


# ── Case 3: same tolerance inside the fused read_core_state path ──────────

def test_read_core_state_tolerates_unsupported_storm_control(monkeypatch):
    """The scope='all' fusion path (VLAN + ports + SVI in one SSH session)
    must degrade the storm slot the same way -- otherwise a refresh of an
    IOSv would fail on the whole device instead of just the storm field."""
    v = CiscoVendor()
    cmds = v._cargar_comandos()
    n_vlan = len(cmds["list_vlans"]["primary"]["commands"])
    n_port = len(cmds["list_ports"]["primary"]["commands"])
    n_svi = len(cmds["get_svis"]["primary"]["commands"])

    vlan_brief = (
        "VLAN Name                             Status    Ports\n"
        "---- -------------------------------- --------- ------\n"
        "10   MGMT                             active    Gi0/1\n"
    )
    # vlan + port commands + svi commands -- the storm-control command is
    # the invalid one; the trailing running-config-per-interface command
    # (added by the storm-control action/trap parametrization, after the
    # storm command in commands.yaml) is stubbed empty, parser tolerates.
    stub_stdouts = (
        [vlan_brief]
        + [_STATUS, _DESC, _SW, _STORM_INVALID] + [""] * (n_port - 4)
        + ["", ""]  # svi running-config + brief -- empty is OK, parser tolerates
    )
    assert len(stub_stdouts) == n_vlan + n_port + n_svi

    def _fake_run(playbook, extravars, inventory=None, device=None):
        return _make_run_result(rc=1, stdouts=stub_stdouts)

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run)

    vlans, ports, svis = v.read_core_state(_FakeDevice(), "pw")

    assert any(vl.vlan_id == 10 for vl in vlans)
    assert len(ports) == 1
    assert ports[0].storm_control_enabled is None


# ── Case 4: opt-in only -- callers that don't pass partial_ok still get the raise ──

# ── Case 5: extractor handles loop shape from Cisco/Huawei playbooks ───────

def test_extractor_handles_loop_results_with_list_or_str_stdout():
    """The tolerant path in cisco/run.yml uses ``loop:`` -- ansible-runner
    emits ONE ``runner_on_ok`` event whose ``res["results"]`` is a list
    of per-iteration dicts. Each dict's ``stdout`` is:
        * list[str] (ios_command)  -> take the first element
        * str        (cli_command) -> append as-is
        * absent / None            -> append "" so the caller's index-based
          slicing stays aligned
    This regression test guards against a change to the extractor that
    would silently drop failed iterations and shift every subsequent
    index by one."""
    from app.services.ansible_service import _extract_all_command_outputs

    class _Runner:
        events = [
            {
                "event": "runner_on_ok",
                "event_data": {
                    "res": {
                        "results": [
                            {"stdout": ["cisco status output"]},   # ios_command loop OK
                            {"stdout": ["cisco desc output"]},
                            {"stdout": ["cisco switchport output"]},
                            {"failed": True, "msg": "% Invalid input"},  # storm rejected, no stdout
                            {"stdout": "huawei-style string stdout"},   # cli_command loop OK
                            {"stdout": []},                              # empty list edge case
                            {},                                          # nothing at all
                        ]
                    }
                },
            }
        ]

    outputs = _extract_all_command_outputs(_Runner())

    # 7 iterations -> exactly 7 outputs, no index drift.
    assert len(outputs) == 7
    assert outputs[0] == "cisco status output"
    assert outputs[1] == "cisco desc output"
    assert outputs[2] == "cisco switchport output"
    assert outputs[3] == ""                             # failed iteration
    assert outputs[4] == "huawei-style string stdout"
    assert outputs[5] == ""                             # empty list
    assert outputs[6] == ""                             # missing dict


# ── Case 6: contract test -- partial_ok flips the playbook into tolerant mode ──

def test_leer_partial_ok_passes_tolerate_flag_to_playbook(monkeypatch):
    """The playbook has two ios_command tasks gated by
    ``tolerate_command_errors``; the strict one is the default and
    surfaces "% Invalid input" as a task failure (needed by
    get_snmp_status and friends), the tolerant one uses ``failed_when:
    false`` so stdout comes back populated even when a command is
    rejected. _leer must set the extravar iff partial_ok=True.

    Regression guard: without this, a future refactor of _leer could
    silently drop the flag and we'd be back to Ansible discarding the
    partial stdouts."""
    captured: list[dict] = []

    def _fake_run(playbook, extravars, inventory=None, device=None):
        captured.append(dict(extravars))
        return _make_run_result(rc=0, stdouts=[""] * 4)

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run)

    v = CiscoVendor()
    v._leer(["show foo"], _FakeDevice(), "pw")  # strict default
    v._leer(["show bar"], _FakeDevice(), "pw", partial_ok=True)  # tolerant

    assert "tolerate_command_errors" not in captured[0]
    assert captured[1].get("tolerate_command_errors") is True


def test_leer_default_behavior_unchanged_on_failure(monkeypatch):
    """Regression guard: callers that DON'T ask for partial_ok (i.e., the
    entire rest of the codebase -- list_vlans, get_svis, set_hostname's
    read side, etc.) must keep raising on rc != 0 as before, even if some
    stdouts came through. Otherwise a real "show vlan brief" failure would
    silently return partial data."""

    def _fake_run(playbook, extravars, inventory=None, device=None):
        return _make_run_result(rc=1, stdouts=["some partial output"])

    monkeypatch.setattr(ansible_service, "run_playbook", _fake_run)

    v = CiscoVendor()
    with pytest.raises(RuntimeError, match="cisco-01"):
        v._leer(["show vlan brief"], _FakeDevice(), "pw")
