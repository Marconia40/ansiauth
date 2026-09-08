"""``resumen_intento()`` -- frase corta legible de la INTENCIÓN de un
request (no el resultado real, eso es ``accion``, post-``aplicar()``).
Reemplaza mostrar el ``asdict()`` crudo del dataclass en el job detail
modal del frontend (``JobDetailModal.tsx`` -> ``Job.parameters_summary``).

Un caso por dataclass (``VLAN``/``SVI``/``Puerto``/``GlobalConfig``) más el
caso "flag especial" (eliminar/crear/reset) de cada una -- no se busca
cobertura exhaustiva de cada mutation_field, solo confirmar que el
mecanismo (mirar mutation_fields, describir cada uno) funciona para cada
tipo y no revienta con los shapes reales (dict anidados, listas de reglas).
"""
from app.models.vlan import VLAN
from app.models.svi import SVI
from app.models.port import Puerto
from app.models.global_config import GlobalConfig


def test_vlan_resumen_intento_create():
    vlan = VLAN(vlan_id=15, name="MGMT")
    assert vlan.resumen_intento() == "VLAN 15: set name to 'MGMT'"


def test_vlan_resumen_intento_delete():
    vlan = VLAN(vlan_id=15, eliminar=True)
    assert vlan.resumen_intento() == "Delete VLAN 15"


def test_svi_resumen_intento_single_field():
    svi = SVI(vlan_id=300, device="f3r9s1", description="uplink")
    assert svi.resumen_intento() == "SVI 300 on f3r9s1: set description to 'uplink'"


def test_svi_resumen_intento_delete():
    svi = SVI(vlan_id=300, device="f3r9s1", eliminar=True)
    assert svi.resumen_intento() == "Delete SVI 300 on f3r9s1"


def test_puerto_resumen_intento_mode_change():
    puerto = Puerto(interface="Gi0/1", device="cisco01", mode="access", access_vlan=10)
    resumen = puerto.resumen_intento()
    assert resumen.startswith("Port Gi0/1 on cisco01: ")
    assert "set access_vlan to 10" in resumen
    assert "set mode to 'access'" in resumen


def test_puerto_resumen_intento_reset():
    puerto = Puerto(interface="Gi0/1", device="cisco01", reset=True)
    assert puerto.resumen_intento() == "Reset Port Gi0/1 on cisco01 to defaults"


def test_global_config_resumen_intento_route_add():
    gc = GlobalConfig(route_add={"destination": "192.168.100.0/24", "next_hop": "10.10.100.1"})
    assert gc.resumen_intento() == "Add route 192.168.100.0/24 -> 10.10.100.1"


def test_global_config_resumen_intento_acl_create():
    gc = GlobalConfig(acl_create={"name": "acceso-snmp", "rules": [{"seq": 5}, {"seq": 10}]})
    assert gc.resumen_intento() == "Create/update ACL 'acceso-snmp' (2 rules)"


def test_global_config_resumen_intento_hostname():
    gc = GlobalConfig(hostname="core-sw-01")
    assert gc.resumen_intento() == "Set hostname to 'core-sw-01'"
