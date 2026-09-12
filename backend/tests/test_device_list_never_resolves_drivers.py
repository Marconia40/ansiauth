"""Building many Device objects (as Inventory.list() would for a table view)
must never trigger driver resolution or password decryption — the whole
point of laziness (DG2). See docs/DEVICE_IMPLEMENTATION_PLAN.md §11.1.

Ported from the pre-FINAL_ARCHITECTURE.md version: Device now caches a
single driver (`._driver`, via app.composition.plugin_registry) and a
single decrypted password (`._password`, via
app.composition.secret_vault) — there's no separate vlan/port driver
split or dispatcher module anymore."""
from app.models.device import Device


def test_building_many_devices_never_resolves_drivers(monkeypatch):
    def _boom_driver(*_a, **_kw):
        raise AssertionError("plugin_registry.obtener must not be called for plain construction")

    def _boom_decrypt(*_a, **_kw):
        raise AssertionError("secret_vault.decrypt must not be called for plain construction")

    monkeypatch.setattr("app.composition.plugin_registry.obtener", _boom_driver)
    monkeypatch.setattr("app.composition.secret_vault.decrypt", _boom_decrypt)

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
    assert all(d._driver is None and d._password is None for d in devices)
