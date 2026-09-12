"""Shared test-only fake vendor driver for Puerto (port) write-path tests.

Not a conftest.py fixture on purpose (conftest.py is off-limits for this
modernization pass) -- imported directly by the individual
``test_port_*``/``test_ports_*`` files that need an end-to-end write
through ``Orquestador.ejecutar()``/``ejecutar_lote()``.

Why this exists instead of ``app.services.vendors.mock.MockVendor``
------------------------------------------------------------------
Confirmed by reading the current source: ``Puerto.aplicar()`` (every
branch -- description, admin_up, access_vlan, allowed_vlans, poe,
storm_control, and the atomic mode branches) computes its device call via
``self._resolver_XXX(...)`` and then invokes
``device.driver.aplicar_paso(op_key, variant, vars, device, password)``,
which is ``VendorDriver._aplicar_desde_template()`` under the hood -- a
YAML-template-driven dispatch that needs both a ``resolver_set_XXX``
method on the driver AND a ``commands.yaml`` file next to the driver
module. ``MockVendor`` (app/services/vendors/mock.py) implements none of
the ``resolver_*`` methods and has no ``commands.yaml`` -- it only
implements the OLD-style direct ``set_XXX(...)`` methods, which today are
called ONLY by ``Puerto.ejecutar_rollback()``/``_ejecutar_rollback_reset()``
(the rollback path), never by the forward ``aplicar()`` path.

Net effect: registering ``MockVendor`` for a device's vendor and then
calling any single ``PATCH/POST .../ports/*`` write endpoint against it
raises ``AttributeError: 'MockVendor' object has no attribute
'resolver_set_port_access_vlan'`` (or the equivalent for whichever field)
instead of a simulated success. This looks like a genuine, real gap in
``app/services/vendors/mock.py`` (EXECUTION_MODE=mock is effectively
broken for every Puerto write operation) -- reported in the modernization
summary, not fixed here (test-only pass).

``FakePuertoDriver`` below fills that gap for tests: it implements the
``resolver_*``/``aplicar_paso``/``aplicar_lote`` contract Puerto.aplicar()
actually needs (bypassing the YAML machinery entirely -- ``aplicar_paso``
just records the call and returns success/failure), AND the direct
``set_XXX``/``reset_port`` methods Puerto's rollback path needs, backed by
a static, non-mutating ``list_ports()`` view (same "always returns the
initial state" behavior as ``MockVendor``, so re-running writes against
the same fake device in different tests stays deterministic and a
post-rollback re-read always matches the pre_state that was captured).
"""
from __future__ import annotations

from app.models.port import Puerto
from app.services.vendors.base import VendorDriver

# Mirrors app.services.vendors.mock.MockVendor's _INITIAL_MOCK_PORTS shape
# (same interfaces/vlans) so tests can reuse the same mental model.
DEFAULT_PORTS: list[Puerto] = [
    Puerto(
        interface="GigabitEthernet0/0/1", description="Workstation-01",
        admin_up=True, operational_up=True, mode="access", access_vlan=10,
    ),
    Puerto(
        interface="GigabitEthernet0/0/2", description="Workstation-02",
        admin_up=True, operational_up=False, mode="access", access_vlan=20,
    ),
    Puerto(
        interface="GigabitEthernet0/0/24", description="Uplink to core",
        admin_up=True, operational_up=True, mode="trunk", access_vlan=1,
        allowed_vlans=[10, 20, 30],
    ),
]


class FakePuertoDriver(VendorDriver):
    """Test double satisfying the parts of ``VendorDriver`` that
    ``Puerto``'s forward-apply AND rollback paths actually call.

    ``fail_on(op_key, vars) -> bool`` -- optional hook, checked by both
    ``aplicar_paso``/``aplicar_lote`` (forward path). Return ``True`` to
    simulate a device-side rejection for that specific call. The
    simulated ``stderr`` always contains ``"invalid input"`` (a
    recognized PERMANENT pattern in ``Orquestador._clasificar_error``),
    so a failing test fails on the first attempt with no retries.

    ``calls`` -- ordered list of ``(op_key, variant, vars)`` for the
    forward path and ``("rollback:<method>", None, vars)`` for direct
    rollback calls -- inspect this to assert what was actually sent to
    "the device".
    """

    def __init__(self, ports: "list[Puerto] | None" = None, fail_on=None):
        self.ports = list(ports) if ports is not None else [
            Puerto.from_dict(p.to_dict()) for p in DEFAULT_PORTS
        ]
        self.fail_on = fail_on
        self.calls: list[tuple[str, "str | None", dict]] = []

    # -- VLAN abstract stubs (unused by port tests, present only to
    #    satisfy VendorDriver's ABC) --
    def create_vlan(self, *a, **k):
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def delete_vlan(self, *a, **k):
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def update_vlan(self, *a, **k):
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def save_config(self, *a, **k):
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def get_vlans(self, *a, **k):
        return []

    def get_svis(self, *a, **k):
        # Not abstract on VendorDriver, but Inventory.register()'s
        # fire-and-forget sync_device_task (eager under
        # CELERY_TASK_ALWAYS_EAGER) calls read_core_state() -> get_svis()
        # -- overridden here purely to keep test logs quiet, no test in
        # this suite asserts on SVIs.
        return []

    # -- port query --
    def list_ports(self, device, password):
        return [Puerto.from_dict(p.to_dict()) for p in self.ports]

    # -- resolver_* (forward path, used by Puerto._resolver_XXX) --
    def resolver_update_port_description(self, interface, description):
        return ("update_port_description", None, {"interface": interface, "description": description})

    def resolver_set_port_admin_state(self, interface, enabled):
        return ("set_port_admin_state", None, {"interface": interface, "enabled": enabled})

    def resolver_set_port_access_vlan(self, interface, vlan_id):
        return ("set_port_access_vlan", None, {"interface": interface, "vlan_id": vlan_id})

    def resolver_set_trunk_pvid_vlan(self, interface, vlan_id):
        return ("set_trunk_pvid_vlan", None, {"interface": interface, "vlan_id": vlan_id})

    def resolver_set_trunk_allowed_vlans(self, interface, vlan_list):
        return ("set_trunk_allowed_vlans", None, {"interface": interface, "vlan_list": list(vlan_list)})

    def resolver_set_port_poe(self, interface, enabled):
        return ("set_port_poe", None, {"interface": interface, "enabled": enabled})

    def resolver_set_storm_control(self, interface, enabled, threshold, action="shutdown", trap=True):
        return ("set_storm_control", None, {
            "interface": interface, "enabled": enabled, "threshold": threshold,
            "action": action, "trap": trap,
        })

    def resolver_reset_port(self, interface):
        return ("reset_port", None, {"interface": interface})

    def resolver_set_access_mode(self, interface, vlan_id):
        return ("set_access_mode", None, {"interface": interface, "vlan_id": vlan_id})

    def resolver_set_trunk_mode(self, interface, native_vlan, vlan_list):
        return ("set_trunk_mode", None, {
            "interface": interface, "native_vlan": native_vlan, "vlan_list": list(vlan_list),
        })

    # -- forward-path dispatch (replaces VendorDriver._aplicar_desde_template,
    #    no commands.yaml needed) --
    def aplicar_paso(self, op_key, variant, vars, device, password):
        self.calls.append((op_key, variant, dict(vars)))
        if self.fail_on and self.fail_on(op_key, vars):
            return {"rc": 1, "stdout": "", "stderr": "invalid input detected -- simulated failure", "success": False}
        return {"rc": 0, "stdout": "ok", "stderr": "", "success": True}

    def aplicar_lote(self, pasos, device, password, *, op_label="lote"):
        for op_key, variant, vars in pasos:
            self.calls.append((op_key, variant, dict(vars)))
            if self.fail_on and self.fail_on(op_key, vars):
                return {"rc": 1, "stdout": "", "stderr": "invalid input detected -- simulated failure", "success": False}
        return {"rc": 0, "stdout": "ok", "stderr": "", "success": True}

    # -- direct set_XXX (rollback path, used by Puerto.ejecutar_rollback()/
    #    _ejecutar_rollback_reset(), and Puerto._aplicar_reset()) --
    def update_port_description(self, interface, description, device, password):
        self.calls.append(("rollback:update_port_description", None, {"interface": interface, "description": description}))
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def set_port_admin_state(self, interface, enabled, device, password):
        self.calls.append(("rollback:set_port_admin_state", None, {"interface": interface, "enabled": enabled}))
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def set_port_access_vlan(self, interface, vlan_id, device, password):
        self.calls.append(("rollback:set_port_access_vlan", None, {"interface": interface, "vlan_id": vlan_id}))
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def set_trunk_pvid_vlan(self, interface, vlan_id, device, password):
        self.calls.append(("rollback:set_trunk_pvid_vlan", None, {"interface": interface, "vlan_id": vlan_id}))
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def set_trunk_allowed_vlans(self, interface, vlan_list, device, password):
        self.calls.append(("rollback:set_trunk_allowed_vlans", None, {"interface": interface, "vlan_list": list(vlan_list)}))
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def set_port_poe(self, interface, enabled, device, password):
        self.calls.append(("rollback:set_port_poe", None, {"interface": interface, "enabled": enabled}))
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def set_storm_control(self, interface, enabled, threshold, action, trap, device, password):
        self.calls.append(("rollback:set_storm_control", None, {
            "interface": interface, "enabled": enabled, "threshold": threshold,
            "action": action, "trap": trap,
        }))
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def reset_port(self, interface, device, password):
        self.calls.append(("rollback:reset_port", None, {"interface": interface}))
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def set_access_mode(self, interface, vlan_id, device, password):
        self.calls.append(("rollback:set_access_mode", None, {"interface": interface, "vlan_id": vlan_id}))
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}

    def set_trunk_mode(self, interface, native_vlan, vlan_list, device, password):
        self.calls.append(("rollback:set_trunk_mode", None, {
            "interface": interface, "native_vlan": native_vlan, "vlan_list": list(vlan_list),
        }))
        return {"rc": 0, "stdout": "", "stderr": "", "success": True}


def get_or_create_site(name: str):
    """Idempotent site lookup/creation -- the Postgres test DB is not reset
    between test functions within a session (only once, session-wide, by
    conftest.py), so a function-scoped fixture that calls
    ``site_repository.crear_con_grupo_default(name, ...)`` unconditionally
    raises ``ValueError('Site already exists')`` on the 2nd+ test in a
    file. Reuse the same site row across every test in a file instead of
    generating a fresh unique name per test."""
    from app.composition import site_repository
    from app.db.models import SiteModel
    from app.db.session import get_session

    with get_session() as session:
        row = session.query(SiteModel.id).filter_by(name=name).first()
    if row is not None:
        return site_repository.get(row[0])
    return site_repository.crear_con_grupo_default(name, kind="REGULAR")


def get_or_create_device(name: str, *, site_name: str, vendor: str = "cisco_ios", host: str = "192.0.2.10"):
    """Idempotent device registration -- same rationale as
    ``get_or_create_site()`` above: re-running a test file (or another
    test in the same file/session) against a device name that already
    exists must fetch the existing row instead of raising."""
    from app.composition import device_repository, inventory
    from app.core.exceptions import ValidationError

    site = get_or_create_site(site_name)
    try:
        return inventory.register(
            name=name, host=host, vendor=vendor, platform="ios" if vendor == "cisco_ios" else "vrp",
            username="admin", password="pw",
            site_id=site.id, device_group_id=site.default_group_id,
            actor={"username": "admin"},
        )
    except ValidationError:
        return device_repository.get(name)


class StubUnimplementedDriver(VendorDriver):
    """Minimal driver that deliberately does NOT override any port
    mutation method -- used to exercise the 501 VENDOR_NOT_SUPPORTED gate
    in app/api/ports.py's ``_require_port_driver_with()`` (checks whether
    the concrete driver class overrides VendorDriver's default
    NotImplementedError stub for a given method name)."""

    def create_vlan(self, *a, **k):
        raise NotImplementedError

    def delete_vlan(self, *a, **k):
        raise NotImplementedError

    def update_vlan(self, *a, **k):
        raise NotImplementedError

    def save_config(self, *a, **k):
        raise NotImplementedError

    def get_vlans(self, *a, **k):
        return []

    def list_ports(self, *a, **k):
        return []
