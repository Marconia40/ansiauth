"""Tests para ``app/services/parsers/arp_mac_parser.py``. No existía un
archivo dedicado -- este es el primero, motivado por un bug real
encontrado en vivo contra f1r2s1 (device Huawei grande, 3080 entradas
MAC reales) que ``parse_huawei_mac()`` no tenía cubierto."""
from __future__ import annotations

from app.services.parsers.arp_mac_parser import parse_huawei_mac


def test_parse_huawei_mac_confirmed_format_sin_age():
    """Formato ya confirmado en vivo (comentario original del regex,
    52/52 filas reales) -- 4 campos después de la MAC, sin columna Age.
    Sigue andando igual después de hacer esa columna opcional."""
    raw = (
        "MAC Address    VLAN/VSI/BD   Learned-From        Type\n"
        "-------------------------------------------------------------------------------\n"
        "0056-2b0f-996c 156/-/-                           GE0/0/48            dynamic\n"
        "-------------------------------------------------------------------------------\n"
        "Total items displayed = 1\n"
    )
    rows = parse_huawei_mac(raw)
    assert len(rows) == 1
    assert rows[0] == {
        "mac": "0056-2b0f-996c", "vlan": "156", "type": "dynamic", "interface": "GE0/0/48",
    }


def test_parse_huawei_mac_with_trailing_age_column():
    """Bug real contra f1r2s1 (device grande, 3080 entradas MAC reales):
    este firmware agrega una 5ta columna 'Age' al final que el regex
    viejo no dejaba pasar (ancla `$` justo después de 'Type') -- 0 de
    3080 filas parseaban, sin ningún error visible (tabla MAC vacía es
    indistinguible de 'el device no tiene entradas' sin mirar el raw).
    Fixture con las filas reales capturadas en vivo."""
    raw = (
        "Flags: * - Backup  \n"
        "       # - forwarding logical interface, operations cannot be performed based \n"
        "           on the interface.\n"
        "BD   : bridge-domain   Age : dynamic MAC learned time in seconds\n"
        "-------------------------------------------------------------------------------\n"
        "MAC Address    VLAN/BD       Learned-From        Type                Age\n"
        "-------------------------------------------------------------------------------\n"
        "000c-2915-bbd8 1/-           Eth-Trunk4          dynamic                 27\n"
        "242f-d0d7-e060 999/-         10GE6/0/31          dynamic                  3\n"
        "dcce-8100-c18f 999/-         10GE6/0/31          dynamic                  2\n"
        "-------------------------------------------------------------------------------\n"
        "Total items: 3\n"
    )
    rows = parse_huawei_mac(raw)
    assert len(rows) == 3
    assert rows[0] == {
        "mac": "000c-2915-bbd8", "vlan": "1", "type": "dynamic", "interface": "Eth-Trunk4",
    }
    assert rows[1]["interface"] == "10GE6/0/31"
    assert rows[2]["mac"] == "dcce-8100-c18f"


def test_parse_huawei_mac_empty_input():
    assert parse_huawei_mac("") == []
