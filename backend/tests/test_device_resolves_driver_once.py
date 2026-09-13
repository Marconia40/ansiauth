"""DG2 regression test: the vendor driver and the decrypted password are
each resolved at most once per Device instance, cached across accesses.
See docs/DEVICE_IMPLEMENTATION_PLAN.md §11.1 / §5 (DG2).

Ported from the pre-FINAL_ARCHITECTURE.md version of this file: Device no
longer has per-operation delegate methods (create_vlan/list_vlans/
list_ports/set_port_admin_state) or a dispatcher module
(app.services.vendors.dispatcher) — it exposes a single lazy-cached
`.driver` property (resolved via app.composition.plugin_registry) and a
single lazy-cached `.password` property (resolved via
app.composition.secret_vault, replacing the deleted secret_service)."""
from app.models.device import Device


def _make_device(**overrides) -> Device:
    defaults = dict(
        name="sw1",
        host="10.0.0.1",
        vendor="huawei_vrp",
        username="admin",
        encrypted_password="enc",
    )
    defaults.update(overrides)
    return Device(**defaults)


def test_driver_resolved_once_across_accesses(monkeypatch):
    calls = []

    def _fake_obtener(vendor):
        calls.append(vendor)
        return object()

    monkeypatch.setattr("app.composition.plugin_registry.obtener", _fake_obtener)

    device = _make_device()
    first = device.driver
    second = device.driver

    assert len(calls) == 1
    assert first is second


def test_password_decrypted_once_across_accesses(monkeypatch):
    decrypt_calls = []

    def _fake_decrypt(enc):
        decrypt_calls.append(enc)
        return "pw"

    monkeypatch.setattr("app.composition.secret_vault.decrypt", _fake_decrypt)

    device = _make_device()
    first = device.password
    second = device.password

    assert len(decrypt_calls) == 1
    assert first == second == "pw"
