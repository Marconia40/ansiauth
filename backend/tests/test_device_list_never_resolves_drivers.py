"""Building many Device objects (as Inventory.list() would for a table view)
must never trigger driver resolution or password decryption — the whole
point of laziness (DG2). See docs/DEVICE_IMPLEMENTATION_PLAN.md §11.1.
"""
from app.models.device import Device


def test_building_many_devices_never_resolves_drivers(monkeypatch):
    monkeypatch.setattr("app.core.config.EXECUTION_MODE", "real")

    def _boom_driver(*_a, **_kw):
        raise AssertionError("get_driver must not be called for plain construction")

    def _boom_port_driver(*_a, **_kw):
        raise AssertionError("get_port_driver must not be called for plain construction")

    def _boom_decrypt(*_a, **_kw):
        raise AssertionError("decrypt_password must not be called for plain construction")

    monkeypatch.setattr("app.services.vendors.dispatcher.get_driver", _boom_driver)
    monkeypatch.setattr("app.services.vendors.dispatcher.get_port_driver", _boom_port_driver)
    monkeypatch.setattr("app.services.secret_service.vault.decrypt", _boom_decrypt)

    devices = [
        Device(
            name=f"sw{i}",
            host=f"10.0.0.{i}",
            vendor="huawei_vrp",
            username="admin",
            encrypted_password="enc",
        )
        for i in range(500)
    ]

    names = [d.name for d in devices]
    hosts = [d.host for d in devices]

    assert len(names) == 500
    assert len(hosts) == 500
    assert all(d._vlan_driver is None and d._port_driver is None for d in devices)
