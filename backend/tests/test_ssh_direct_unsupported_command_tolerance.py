"""Espejo de ``test_cisco_unsupported_command_tolerance.py`` pero para el
transporte ``ssh_direct_service`` (``auth_method="key"``) en vez de
Ansible/password.

Motivo: esta sesión consolidó ``ssh_direct_service._run_reads()`` para
mandar todos los comandos de un read por UNA sola sesión SSH (antes abría
una conexión nueva por comando -- confirmado en producción como ~24
conexiones en 16s para un solo read de 8 comandos contra un device con
algoritmos SSH legacy, la clase de ráfaga que puede empujar a un device
con firmware viejo a resetear la conexión). El contrato de salida no
cambió (``_extraer_salidas_comandos()`` sigue devolviendo N strings en
el mismo orden que *commands*, con "" para un comando sin datos), así que
``VendorDriver._filter_unsupported()`` + los parsers no deberían
necesitar ningún cambio -- este archivo prueba esa promesa end-to-end
contra el transporte por clave, que hasta ahora sólo se probaba indirecto
via ``_run_ssh_interactive()``/``_run_reads()`` en aislamiento
(``test_mac_table_search.py``), nunca a través de un driver real completo
como ya se hacía para Ansible."""
from __future__ import annotations

import pytest

from app.services import ssh_direct_service
from app.services.vendors.cisco.driver import CiscoVendor
from app.services.vendors.huawei.driver import HuaweiVendor


class _NullAgentCtx:
    def __enter__(self):
        return {}

    def __exit__(self, *a):
        return False


class _FakeKeyDevice:
    name = "cisco-01"
    host = "192.0.2.10"
    username = "admin"
    vendor = "cisco_ios"
    auth_method = "key"


def _fake_run_factory(transcript: str):
    def _fake_run(cmd, input, capture_output, text, timeout, env):
        class _Proc:
            returncode = 0
            stdout = transcript
            stderr = ""
        return _Proc()
    return _fake_run


# ── Cisco: storm-control rechazado a mitad del batch fusionado ────────────

def test_cisco_list_ports_tolerates_unsupported_storm_control_via_ssh_direct(monkeypatch):
    """Mismo caso que ``test_list_ports_tolerates_unsupported_storm_control``
    (archivo Ansible) pero con los 5 comandos de ``list_ports`` mandados
    por UNA sola sesión SSH -- el device rechaza ``show storm-control
    broadcast`` (comando #4 de 5) a mitad del transcript combinado."""
    transcript = (
        "cisco-01#terminal length 0\n"
        "cisco-01#terminal width 0\n"
        "cisco-01#show interfaces status\n"
        "Port      Name               Status       Vlan       Duplex  Speed Type\n"
        "Gi0/1                        connected    10         a-full  a-1000 10/100/1000BaseTX\n"
        "cisco-01#show interfaces description\n"
        "Interface                      Status         Protocol Description\n"
        "Gi0/1                          up             up       \n"
        "cisco-01#show interfaces switchport\n"
        "Name: Gi0/1\n"
        "Switchport: Enabled\n"
        "Administrative Mode: static access\n"
        "Operational Mode: static access\n"
        "Access Mode VLAN: 10 (MGMT)\n"
        "Trunking Native Mode VLAN: 1 (default)\n"
        "Trunking VLANs Enabled: ALL\n"
        "cisco-01#show storm-control broadcast\n"
        " ^\n"
        "% Invalid input detected at '^' marker.\n"
        "\n"
        "cisco-01#show running-config | section ^interface\n"
        "interface GigabitEthernet0/1\n"
        " switchport access vlan 10\n"
        " switchport mode access\n"
        "cisco-01#quit"
    )
    monkeypatch.setattr(ssh_direct_service.subprocess, "run", _fake_run_factory(transcript))
    monkeypatch.setattr(ssh_direct_service, "_agent_for", lambda device: _NullAgentCtx())

    ports = CiscoVendor().list_ports(_FakeKeyDevice(), "pw")

    assert len(ports) == 1
    p = ports[0]
    assert p.interface == "Gi0/1"
    assert p.mode == "access"
    assert p.access_vlan == 10
    assert p.storm_control_enabled is None
    assert p.storm_control_threshold is None


# ── Huawei: los 4 comandos de list_ports fusionados en 1 sesión ───────────

class _FakeKeyDeviceHuawei(_FakeKeyDevice):
    name = "huawei-01"
    vendor = "huawei_vrp"


def test_huawei_list_ports_parses_correctly_via_single_fused_ssh_session(monkeypatch):
    """``list_ports`` de Huawei manda 4 comandos (``display interface
    brief`` / ``description`` / ``port vlan`` / ``current-configuration
    interface`` -- este último es un dump bulk, no un comando que
    normalmente se rechace como "no soportado" como sí pasa con el
    storm-control dedicado de Cisco, así que este test confirma
    end-to-end que ``_extraer_salidas_comandos()`` separa bien los 4
    outputs de UNA sola sesión SSH fusionada y el parser real de Huawei
    los combina igual que ya probaba ``test_port_parser.py`` por
    separado."""
    transcript = (
        "<huawei-01>screen-length 0 temporary\n"
        "<huawei-01>display interface brief\n"
        "Interface                   PHY     Protocol  InUti OutUti   inErrors  outErrors\n"
        "GigabitEthernet0/0/1        up      up        0.01%  0.01%        0        0\n"
        "<huawei-01>display interface description\n"
        "Interface                  PHY      Protocol Description\n"
        "GigabitEthernet0/0/1       up       up       Workstation01\n"
        "<huawei-01>display port vlan\n"
        "Port                    Link Type    PVID  Trunk VLAN List\n"
        "GigabitEthernet0/0/1    access       10    -\n"
        "<huawei-01>display current-configuration interface\n"
        "<huawei-01>quit"
    )
    monkeypatch.setattr(ssh_direct_service.subprocess, "run", _fake_run_factory(transcript))
    monkeypatch.setattr(ssh_direct_service, "_agent_for", lambda device: _NullAgentCtx())

    ports = HuaweiVendor().list_ports(_FakeKeyDeviceHuawei(), "pw")

    assert len(ports) == 1
    p = ports[0]
    assert p.interface == "GE0/0/1"
    assert p.description == "Workstation01"
    assert p.mode == "access"
    assert p.access_vlan == 10
    assert p.admin_up is True
    assert p.operational_up is True
