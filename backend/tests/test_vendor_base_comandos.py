"""Tests for ``_comandos_desde_extravars()``/``VendorDriver._aplicar()``'s
``commands`` field -- surfaces exactly what was sent to the device (from
``extravars``, known before either transport runs) instead of forcing the
frontend to parse the ansible-playbook console dump (Cisco's ``ios_config``
exposes no ``stdout`` at all) or the device's own raw session echo
(Huawei's ``cli_command`` does return ``stdout``, but it's the full VTY
transcript with banners and every prompt/command echoed back) -- both
equally unreadable in the job-detail UI, for different reasons.
"""
from __future__ import annotations

from app.services.vendors.base import VendorDriver, _comandos_desde_extravars


class _StubDriver(VendorDriver):
    """Minimal concrete VendorDriver -- only ``_aplicar()`` is exercised
    here, the rest of the abstract surface just needs to exist to allow
    instantiation."""

    _PLAYBOOK = "stub.yml"
    _NETWORK_OS = "stub"

    def create_vlan(self, *a, **kw): return {}
    def delete_vlan(self, *a, **kw): return {}
    def update_vlan(self, *a, **kw): return {}
    def save_config(self, *a, **kw): return {}
    def get_vlans(self, device, password): return []
    def list_ports(self, device, password): return []


class _FakeDevice:
    name = "stub-device"


# ── _comandos_desde_extravars(): las 5 shapes + fallback ────────────────────

def test_lines_con_parents():
    extravars = {"lines": ["switchport access vlan 20"], "parents": "interface Gi1/0/2"}
    assert _comandos_desde_extravars(extravars) == [
        "interface Gi1/0/2", "switchport access vlan 20",
    ]


def test_lines_sin_parents():
    extravars = {"lines": ["hostname sw1"]}
    assert _comandos_desde_extravars(extravars) == ["hostname sw1"]


def test_command_block_huawei():
    extravars = {"command_block": "system-view\ninterface Eth-Trunk1\ncommit\nquit\nquit"}
    assert _comandos_desde_extravars(extravars) == [
        "system-view", "interface Eth-Trunk1", "commit", "quit", "quit",
    ]


def test_commands_bare_exec():
    extravars = {"commands": ["write memory"]}
    assert _comandos_desde_extravars(extravars) == ["write memory"]


def test_command_blocks_lote_huawei():
    extravars = {"command_blocks": ["system-view\nquit", "interface Eth-Trunk2\ncommit\nquit"]}
    assert _comandos_desde_extravars(extravars) == [
        "system-view", "quit", "interface Eth-Trunk2", "commit", "quit",
    ]


def test_config_steps_lote_cisco():
    extravars = {"config_steps": [
        {"parents": "interface Gi1/0/1", "lines": ["switchport access vlan 10"]},
        {"lines": ["hostname sw1"]},  # sin parents -- no todo step de un lote los tiene
    ]}
    assert _comandos_desde_extravars(extravars) == [
        "interface Gi1/0/1", "switchport access vlan 10", "hostname sw1",
    ]


def test_shape_desconocida_da_vacio():
    assert _comandos_desde_extravars({}) == []
    assert _comandos_desde_extravars({"match": "line"}) == []


# ── VendorDriver._aplicar(): el campo llega al resultado normalizado ────────

def test_aplicar_agrega_commands_shape_cisco(monkeypatch):
    driver = _StubDriver()
    monkeypatch.setattr(
        driver, "_ejecutar",
        lambda extravars, device, password: {"rc": 0, "stdout": "", "stderr": ""},
    )
    resultado = driver._aplicar(
        {"lines": ["switchport access vlan 20"], "parents": "interface Gi1/0/2"},
        _FakeDevice(), "pw", op_label="test",
    )
    assert resultado["success"] is True
    assert resultado["commands"] == ["interface Gi1/0/2", "switchport access vlan 20"]


def test_aplicar_agrega_commands_shape_huawei(monkeypatch):
    driver = _StubDriver()
    monkeypatch.setattr(
        driver, "_ejecutar",
        lambda extravars, device, password: {"rc": 0, "stdout": "[f3r9s2]...", "stderr": ""},
    )
    resultado = driver._aplicar(
        {"command_block": "system-view\ninterface Eth-Trunk1\ncommit\nquit\nquit"},
        _FakeDevice(), "pw", op_label="test",
    )
    assert resultado["success"] is True
    assert resultado["commands"] == [
        "system-view", "interface Eth-Trunk1", "commit", "quit", "quit",
    ]


def test_aplicar_sin_commands_cuando_shape_no_reconocida(monkeypatch):
    driver = _StubDriver()
    monkeypatch.setattr(
        driver, "_ejecutar",
        lambda extravars, device, password: {"rc": 0, "stdout": "", "stderr": ""},
    )
    resultado = driver._aplicar({}, _FakeDevice(), "pw", op_label="test")
    assert "commands" not in resultado


def test_aplicar_agrega_commands_tambien_en_fallo(monkeypatch):
    """El campo no depende de rc/success -- sirve igual de diagnóstico
    cuando el device rechazó el cambio (saber qué se intentó mandar)."""
    driver = _StubDriver()
    monkeypatch.setattr(
        driver, "_ejecutar",
        lambda extravars, device, password: {"rc": 1, "stdout": "", "stderr": "% Invalid input"},
    )
    resultado = driver._aplicar(
        {"lines": ["switchport access vlan 9999"], "parents": "interface Gi1/0/1"},
        _FakeDevice(), "pw", op_label="test",
    )
    assert resultado["success"] is False
    assert resultado["commands"] == ["interface Gi1/0/1", "switchport access vlan 9999"]
