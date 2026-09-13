"""Bug real encontrado en vivo contra f3r9s2 (access -> trunk real, con
tráfico real de por medio): ``set_trunk_mode`` nunca mandaba la "y" que
contesta el prompt ``[Y/N]`` de VRP para "port link-type trunk". Sin
ella, VRP contestó el prompt como "N" sola (sin que nadie la mandara),
"port link-type trunk" nunca se aplicó, y el resto del bloque
(``port trunk pvid vlan``/``undo port trunk allow-pass``/``port trunk
allow-pass vlan``) falló en cascada con "Unrecognized command" -- el
puerto quedó a medio camino en access en vez de trunk.

A diferencia de ``set_access_mode`` (donde la "y" es condicional al
pre-state real del puerto), acá se manda siempre en el primary -- no
hay evidencia de que sea condicional para trunk -- con una alternativa
que reintenta sin ella si el device la rechaza (mismo patrón, mismo
regex, ya validado en vivo para ``set_access_mode`` contra huawei01).
"""
from __future__ import annotations

from app.services.vendors.huawei.driver import HuaweiVendor


def test_primary_incluye_la_y():
    comandos = HuaweiVendor()._cargar_comandos()["set_trunk_mode"]
    assert "y" in comandos["primary"]["block"]


def test_alternativa_sin_y_para_devices_que_la_rechazan():
    comandos = HuaweiVendor()._cargar_comandos()["set_trunk_mode"]
    alternativas = comandos["alternatives"]
    triggers = [a["triggered_by_error"] for a in alternativas]
    assert any("Unrecognized command" in t and r"\]y" in t for t in triggers)
    alt = next(a for a in alternativas if r"\]y" in a["triggered_by_error"])
    assert "y" not in alt["block"]
    # El resto del bloque (salvo la "y") es idéntico al primary.
    assert [l for l in comandos["primary"]["block"] if l != "y"] == alt["block"]


def test_regex_alternativa_matchea_el_error_real_observado():
    import re
    comandos = HuaweiVendor()._cargar_comandos()["set_trunk_mode"]
    alt = next(a for a in comandos["alternatives"] if r"\]y" in a["triggered_by_error"])
    # Texto real capturado en vivo contra f3r9s2 al intentar restaurar
    # GE0/0/2 de access a trunk sin la "y".
    error_real = (
        "[f3r9s2-GigabitEthernet0/0/2]port link-type trunk\r\n"
        "Warning: This command will delete VLANs on this port. Continue?[Y/N]:n\r\n"
        "[f3r9s2-GigabitEthernet0/0/2]undo port trunk allow-pass vlan all\r\n"
        "                                       ^\r\n"
        "Error: Unrecognized command found at '^' position.\r\n"
        "[f3r9s2-GigabitEthernet0/0/2]"
    )
    # Nota: este error real puntual no matchea el regex de "y rechazada"
    # (acá la "y" ni se mandó -- el prompt quedó sin respuesta y contestó
    # "n" solo). El regex cubre el otro caso real ya confirmado
    # (huawei01/set_access_mode): cuando SÍ se manda la "y" y el device
    # la rechaza como comando suelto porque no había prompt esperándola.
    error_y_rechazada = (
        "[*HUAWEI-GE1/0/2]port link-type trunk\r\n"
        "Warning: If this command is executed successfully, VLANs allowed "
        "to pass through this interface will be deleted.\r\n"
        "[*HUAWEI-GE1/0/2]y\r\n"
        "                 ^\r\n"
        "Error: Unrecognized command found at '^' position.\r\n"
        "[*HUAWEI-GE1/0/2]"
    )
    assert not re.search(alt["triggered_by_error"], error_real)
    assert re.search(alt["triggered_by_error"], error_y_rechazada)


def test_alternativas_de_interface_y_commit_tambien_incluyen_la_y():
    """Las otras 2 alternativas (nombre de interfaz completo / commit no
    soportado) parten del mismo primary con "y" -- si el device
    necesitaba la "y" para llegar hasta el error que dispara esa
    alternativa, la sigue necesitando en el reintento."""
    comandos = HuaweiVendor()._cargar_comandos()["set_trunk_mode"]
    for alt in comandos["alternatives"]:
        if r"\]y" in alt["triggered_by_error"]:
            continue  # esta es la que específicamente saca la "y"
        assert "y" in alt["block"], alt["triggered_by_error"]
