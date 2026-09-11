"""``Puerto.storm_control_action``/``storm_control_trap`` -- qué le pasa al
puerto ante una tormenta ("filter"/"shutdown") y si manda trap SNMP,
antes hardcodeados por vendor. Cubre las 4 partes nuevas: la regla de
validación cruzada, el no-op check (antes inexistente), el cálculo de
``action_lines`` en ambos drivers, y los 2 parsers de lectura
(Cisco vía running-config, Huawei vía current-configuration)."""
import pytest

from app.models.port import Puerto
from app.services.parsers.port_parser import parse_ios_storm_control_actions, parse_vrp_storm_control
from app.services.vendors.cisco.driver import CiscoVendor
from app.services.vendors.huawei.driver import HuaweiVendor


# ── Puerto.validar() ─────────────────────────────────────────────────────────

def test_action_sin_enabled_true_falla():
    with pytest.raises(ValueError, match="storm_control_enabled=True"):
        Puerto(interface="Gi1/0/1", storm_control_enabled=False, storm_control_action="shutdown").validar()


def test_trap_sin_enabled_true_falla():
    with pytest.raises(ValueError, match="storm_control_enabled=True"):
        Puerto(interface="Gi1/0/1", storm_control_trap=True).validar()


def test_action_invalido_falla():
    with pytest.raises(ValueError, match="'filter' or 'shutdown'"):
        Puerto(interface="Gi1/0/1", storm_control_enabled=True, storm_control_threshold=10, storm_control_action="bogus").validar()


def test_action_con_enabled_true_es_valido():
    Puerto(
        interface="Gi1/0/1", storm_control_enabled=True, storm_control_threshold=10,
        storm_control_action="filter", storm_control_trap=False,
    ).validar()


# ── No-op check (_resolver_storm_control) ───────────────────────────────────

class _FakeDriver:
    def resolver_set_storm_control(self, *args, **kwargs):
        return ("set_storm_control", "enabled", {})


class _FakeDevice:
    driver = _FakeDriver()


def test_mismo_estado_incluyendo_defaults_es_noop():
    actual = Puerto(
        interface="Gi1/0/1", storm_control_enabled=True, storm_control_threshold=10.0,
        storm_control_action="shutdown", storm_control_trap=True,
    )
    # No especifica action/trap -- se resuelven a los mismos defaults
    # ("shutdown"/True) que ya tenía `actual` -> debería ser no-op.
    pedido = Puerto(interface="Gi1/0/1", storm_control_enabled=True, storm_control_threshold=10.0)
    assert pedido._resolver_storm_control(_FakeDevice(), actual) is None


def test_cambiar_action_no_es_noop():
    actual = Puerto(
        interface="Gi1/0/1", storm_control_enabled=True, storm_control_threshold=10.0,
        storm_control_action="shutdown", storm_control_trap=True,
    )
    pedido = Puerto(
        interface="Gi1/0/1", storm_control_enabled=True, storm_control_threshold=10.0,
        storm_control_action="filter",
    )
    assert pedido._resolver_storm_control(_FakeDevice(), actual) is not None


def test_sin_actual_nunca_es_noop():
    pedido = Puerto(interface="Gi1/0/1", storm_control_enabled=True, storm_control_threshold=10.0)
    assert pedido._resolver_storm_control(_FakeDevice(), None) is not None


# ── Drivers: action_lines ────────────────────────────────────────────────────

@pytest.mark.parametrize("action,trap,expected", [
    ("filter", False, []),
    ("filter", True, ["storm-control action trap"]),
    ("shutdown", False, ["storm-control action shutdown"]),
    ("shutdown", True, ["storm-control action shutdown", "storm-control action trap"]),
])
def test_cisco_action_lines(action, trap, expected):
    _, _, vars = CiscoVendor().resolver_set_storm_control("Gi1/0/1", True, 10.0, action, trap)
    assert vars["action_lines"] == expected


@pytest.mark.parametrize("action,trap,espacio,guion", [
    ("filter", False, ["storm control action block"], ["storm-control action block"]),
    ("filter", True,
     ["storm control action block", "storm control enable trap"],
     ["storm-control action block", "storm-control enable trap"]),
    ("shutdown", False, ["storm control action shutdown"], ["storm-control action shutdown"]),
    ("shutdown", True,
     ["storm control action shutdown", "storm control enable trap"],
     ["storm-control action shutdown", "storm-control enable trap"]),
])
def test_huawei_action_lines(action, trap, espacio, guion):
    _, _, vars = HuaweiVendor().resolver_set_storm_control("GE0/0/1", True, 10.0, action, trap)
    assert vars["action_lines"] == espacio
    assert vars["action_lines_dash"] == guion


# ── Parsers ──────────────────────────────────────────────────────────────────

def test_cisco_running_config_parser():
    cfg = (
        "interface GigabitEthernet1/0/1\n"
        " storm-control broadcast level 10.00\n"
        " storm-control action shutdown\n"
        " storm-control action trap\n"
        "!\n"
        "interface GigabitEthernet1/0/2\n"
        " storm-control broadcast level 5.00\n"
        "!\n"
    )
    rows = parse_ios_storm_control_actions(cfg)
    assert rows["Gi1/0/1"] == ("shutdown", True)
    # Sin línea de action explícita -- el caller (CiscoPortParser.parse_ports)
    # aplica el default "filter", no este parser (ver su docstring).
    assert "Gi1/0/2" not in rows


def test_vrp_storm_control_parser_action_y_trap():
    cfg = (
        "#\n"
        "interface GigabitEthernet0/0/5\n"
        " storm control broadcast min-rate percent 10 max-rate percent 10\n"
        " storm control action block\n"
        " storm control enable trap\n"
        "#\n"
        "interface GigabitEthernet0/0/6\n"
        " storm control broadcast min-rate percent 5 max-rate percent 5\n"
        "#\n"
    )
    rows = parse_vrp_storm_control(cfg)
    assert rows["GE0/0/5"].action == "filter"
    assert rows["GE0/0/5"].trap is True
    # Sin línea de action/trap -- default "filter"/False (sí lo resuelve
    # este parser, a diferencia de Cisco -- ver docstring de _StormRow).
    assert rows["GE0/0/6"].action == "filter"
    assert rows["GE0/0/6"].trap is False


def test_vrp_storm_control_parser_shutdown_dash_form():
    cfg = (
        "#\n"
        "interface GigabitEthernet0/0/7\n"
        " storm-control broadcast min-rate percent 10 max-rate percent 10\n"
        " storm-control action shutdown\n"
        "#\n"
    )
    rows = parse_vrp_storm_control(cfg)
    assert rows["GE0/0/7"].action == "shutdown"
    assert rows["GE0/0/7"].trap is False
