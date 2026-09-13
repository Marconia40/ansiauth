"""Device.driver resolution to MockVendor, and password-decryption timing.

Modernized against the current architecture:

* `app.services.vendors.mock.MockPortDriver`/`MockVlanDriver` are gone --
  fused into one `MockVendor` class (`app/services/vendors/mock.py`).
  `Device` now exposes a SINGLE `.driver` property (no more
  `._vlan_driver`/`._port_driver` split), and has no `create_vlan()`/
  `list_ports()` convenience methods of its own any more -- callers go
  through `device.driver.create_vlan(vlan_id, name, device, password)` /
  `device.driver.list_ports(device, password)` directly.

* CRITICAL, confirmed by reading `app/composition.py`:
  `monkeypatch.setattr("app.core.config.EXECUTION_MODE", "mock")` (the
  old test's approach) has NO EFFECT any more. `plugin_registry` is
  built exactly ONCE, at process import time, by
  `build_plugin_registry()`, which reads `EXECUTION_MODE` at that
  moment and registers either `MockVendor` (both vendor keys) or the
  real `CiscoVendor`/`HuaweiVendor` permanently for the life of the
  process -- there is no more per-call/lazy branch on `EXECUTION_MODE`
  anywhere. This repo's `.env` sets `EXECUTION_MODE=real`, so the
  registry already holds the REAL drivers for the whole pytest session
  regardless of what a test monkeypatches afterwards.

  The correct modern equivalent (used below): swap the registry entry
  itself (`monkeypatch.setitem(plugin_registry._vendors, vendor,
  MockVendor())`), which exercises the exact mechanism that decides
  `Device.driver` resolution today (a flat dict lookup), rather than a
  branching mechanism that no longer exists.

* "mock mode never decrypts the password" (the old test's second claim)
  does NOT hold under the current architecture, verified below rather
  than assumed: `Device.password` is decrypted lazily but INDEPENDENTLY
  of `Device.driver` -- every current call site invokes a driver method
  as `device.driver.some_method(..., device, device.password)`, and
  Python evaluates `device.password` as a plain positional argument
  BEFORE the call happens, regardless of whether the resolved driver is
  `MockVendor` or a real vendor driver, and regardless of whether the
  driver implementation actually uses the `password` parameter.
  `MockVendor`'s methods accept a `password` parameter but never read
  it -- yet the CALLER (whoever writes `device.driver.list_ports(device,
  device.password)`) still triggers the decrypt just by referencing the
  attribute. This is a genuine, confirmed behavior difference from the
  old test's premise (not a mistake in this file) -- reported below
  instead of forcing a false-green assertion.
"""

from __future__ import annotations

from app.composition import plugin_registry
from app.models.device import Device
from app.services import secret_vault as secret_vault_module
from app.services.vendors.mock import MockVendor


def _make_device(**overrides) -> Device:
    defaults = dict(
        name="sw1", host="10.0.0.1", vendor="huawei_vrp",
        username="admin", encrypted_password="enc",
    )
    defaults.update(overrides)
    return Device(**defaults)


def test_registered_mock_vendor_resolves_as_device_driver(monkeypatch):
    """Swapping the registry entry for a vendor to `MockVendor` (the
    mechanism that actually decides resolution today) makes
    `Device.driver` resolve to it -- single `.driver` property, no more
    `._vlan_driver`/`._port_driver` split."""
    mv = MockVendor()
    monkeypatch.setitem(plugin_registry._vendors, "huawei_vrp", mv)

    device = _make_device()
    assert device.driver is mv
    assert isinstance(device.driver, MockVendor)


def test_mock_vendor_list_ports_returns_data(monkeypatch):
    mv = MockVendor()
    monkeypatch.setitem(plugin_registry._vendors, "huawei_vrp", mv)

    device = _make_device()
    ports = device.driver.list_ports(device, "unused-password")
    assert len(ports) > 0


def test_driver_resolution_alone_never_touches_password(monkeypatch):
    """Merely resolving `.driver` (without calling any method that takes
    a password argument) must not decrypt anything -- `.driver` and
    `.password` are independent lazy properties."""
    monkeypatch.setitem(plugin_registry._vendors, "huawei_vrp", MockVendor())

    def _boom(*_a, **_kw):
        raise AssertionError("decrypt must not run just from accessing .driver")
    monkeypatch.setattr(secret_vault_module.vault, "decrypt", _boom)

    device = _make_device()
    _ = device.driver
    assert device._password is None  # still unresolved


def test_password_is_decrypted_once_a_driver_call_actually_needs_it(monkeypatch):
    """Confirmed finding (see module docstring): calling ANY driver
    method that takes `password` as an argument decrypts it, even
    against `MockVendor`, because the caller passes `device.password`
    positionally -- there is no "mock mode skips decryption" branch
    anywhere in the current code. This replaces the old (now-false)
    `test_mock_mode_never_decrypts_password`."""
    monkeypatch.setitem(plugin_registry._vendors, "huawei_vrp", MockVendor())

    calls = []

    def _fake_decrypt(enc):
        calls.append(enc)
        return "decrypted-pw"
    monkeypatch.setattr(secret_vault_module.vault, "decrypt", _fake_decrypt)

    device = _make_device()
    device.driver.list_ports(device, device.password)

    assert calls == ["enc"]
    assert device._password == "decrypted-pw"
