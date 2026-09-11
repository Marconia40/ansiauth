"""``SVI.ipv4_secondary_add``/``ipv4_secondary_remove`` -- agregar/quitar 1
dirección IPv4 secundaria puntual, sin tocar las demás ya configuradas
(a diferencia de DHCP relay, que es full-replace del lado del device).

No hay device real acá -- ``_resolver_ipv4_secondary_add``/``_remove``
solo necesitan que ``device.driver`` tenga
``resolver_set_svi_ipv4_secondary()``, así que un driver fake alcanza
para probar la lógica de precondición/no-op/tope sin levantar nada."""
import pytest

from app.models.svi import SVI, _MAX_IPV4_SECONDARY


class _FakeDriver:
    def resolver_set_svi_ipv4_secondary(self, vlan_id, ipv4_address, previous_ipv4_address):
        variant = "set" if ipv4_address else "clear"
        addr = ipv4_address or previous_ipv4_address
        return ("set_svi_ipv4_secondary", variant, {"vlan_id": vlan_id, "ipv4_addr": addr})


class _FakeDevice:
    driver = _FakeDriver()


def _con_primaria(*secundarias: str) -> SVI:
    return SVI(vlan_id=10, ipv4_address="10.0.0.1/24", ipv4_address_secondary=list(secundarias) or None)


def test_add_nueva_secundaria():
    actual = _con_primaria("10.0.0.2/24")
    op_key, variant, vars = SVI(vlan_id=10, ipv4_secondary_add="10.0.0.5/24")._resolver_ipv4_secondary_add(
        _FakeDevice(), actual,
    )
    assert (op_key, variant, vars["ipv4_addr"]) == ("set_svi_ipv4_secondary", "set", "10.0.0.5/24")


def test_add_duplicada_es_noop():
    actual = _con_primaria("10.0.0.2/24")
    paso = SVI(vlan_id=10, ipv4_secondary_add="10.0.0.2/24")._resolver_ipv4_secondary_add(_FakeDevice(), actual)
    assert paso is None


def test_add_sin_primaria_configurada_falla():
    sin_primaria = SVI(vlan_id=10)
    with pytest.raises(ValueError, match="no primary IPv4"):
        SVI(vlan_id=10, ipv4_secondary_add="10.0.0.5/24")._resolver_ipv4_secondary_add(_FakeDevice(), sin_primaria)


def test_add_en_el_tope_falla():
    actual = _con_primaria(*[f"10.0.0.{i}/24" for i in range(2, 2 + _MAX_IPV4_SECONDARY)])
    with pytest.raises(ValueError, match=f"limit of {_MAX_IPV4_SECONDARY}"):
        SVI(vlan_id=10, ipv4_secondary_add="10.0.0.99/24")._resolver_ipv4_secondary_add(_FakeDevice(), actual)


def test_remove_existente():
    actual = _con_primaria("10.0.0.2/24", "10.0.0.3/24")
    op_key, variant, vars = SVI(vlan_id=10, ipv4_secondary_remove="10.0.0.2/24")._resolver_ipv4_secondary_remove(
        _FakeDevice(), actual,
    )
    assert (op_key, variant, vars["ipv4_addr"]) == ("set_svi_ipv4_secondary", "clear", "10.0.0.2/24")


def test_remove_ausente_es_noop():
    actual = _con_primaria("10.0.0.2/24")
    paso = SVI(vlan_id=10, ipv4_secondary_remove="10.0.0.99/24")._resolver_ipv4_secondary_remove(
        _FakeDevice(), actual,
    )
    assert paso is None


def test_add_y_remove_juntos_son_mutuamente_excluyentes():
    svi = SVI(vlan_id=10, ipv4_secondary_add="10.0.0.5/24", ipv4_secondary_remove="10.0.0.2/24")
    with pytest.raises(ValueError, match="cannot set ipv4_secondary_add and ipv4_secondary_remove"):
        svi.validar()
